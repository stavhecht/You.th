"""Apply SQL migrations without needing the `psql` client.

    python -m scripts.migrate              # apply pending migrations
    python -m scripts.migrate --status     # show what is applied

The backend already depends on asyncpg, so this needs nothing beyond the venv.
Requiring `psql` as well meant installing a whole Postgres client distribution
purely to run one file.

Two things this does that a plain file read would not:

* **Substitutes ``:embedding_dim``.** The vector column width has to match
  ``settings.embedding_dim``; templating it here keeps the two from drifting.
* **Strips psql meta-commands** (the ``\\set`` / ``\\if`` lines). Those are
  client directives, not SQL, and the server rejects them. The migration stays
  runnable under `psql` too — this just ignores the parts only psql understands.

Applied migrations are tracked in ``schema_migrations`` so re-running is safe.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import re
import sys
from pathlib import Path

import asyncpg

from app.config import get_settings

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

#: psql client directives: `\set`, `\if`, `\else`, `\endif`, `\echo`...
_META_COMMAND_RE = re.compile(r"^\s*\\", re.MULTILINE)

TRACKING_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename    TEXT PRIMARY KEY,
    checksum    TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def to_asyncpg_dsn(database_url: str) -> str:
    """SQLAlchemy URL -> plain libpq DSN."""
    return database_url.replace("postgresql+asyncpg://", "postgresql://")


def prepare_sql(raw: str, embedding_dim: int) -> str:
    """Strip psql directives and substitute the templated dimension."""
    without_meta = "\n".join(
        "" if _META_COMMAND_RE.match(line) else line for line in raw.splitlines()
    )
    # `vector(:embedding_dim)` -> `vector(1024)`. Word boundary so a column
    # named e.g. `:embedding_dimension` would not be partially replaced.
    return re.sub(r":embedding_dim\b", str(embedding_dim), without_meta)


def discover() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def checksum(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


async def run(status_only: bool = False) -> int:
    settings = get_settings()
    dsn = to_asyncpg_dsn(settings.database_url)

    try:
        connection = await asyncpg.connect(dsn)
    except (OSError, asyncpg.PostgresError) as exc:
        print(f"Could not connect to {_safe_dsn(dsn)}\n  {exc}", file=sys.stderr)
        print(
            "\nIs Postgres running? With Docker Desktop started:\n"
            "  docker compose up -d db",
            file=sys.stderr,
        )
        return 1

    try:
        await connection.execute(TRACKING_TABLE)
        applied = {
            row["filename"]: row["checksum"]
            for row in await connection.fetch(
                "SELECT filename, checksum FROM schema_migrations"
            )
        }

        migrations = discover()
        if not migrations:
            print(f"No .sql files in {MIGRATIONS_DIR}")
            return 0

        # One runner at a time: two `migrate` containers starting together would
        # both see a file as pending and both apply it. Released on disconnect.
        await connection.execute("SELECT pg_advisory_lock(hashtext('youth_migrate'))")

        pending = 0
        changed = 0
        for path in migrations:
            sql = prepare_sql(path.read_text(), settings.embedding_dim)
            digest = checksum(sql)
            name = path.name

            if name in applied:
                if applied[name] != digest:
                    # The file changed after being applied. Silently re-running
                    # it would be worse than stopping: migrations are not
                    # idempotent, and the database no longer matches the file.
                    print(
                        f"  ! {name}  ALREADY APPLIED, but the file has changed.\n"
                        f"    Write a new migration rather than editing this one.",
                        file=sys.stderr,
                    )
                    changed += 1
                else:
                    print(f"  = {name}  already applied")
                continue

            pending += 1
            if status_only:
                print(f"  + {name}  pending")
                continue

            print(f"  + {name}  applying (embedding_dim={settings.embedding_dim})…")
            try:
                # The file manages its own BEGIN/COMMIT. asyncpg's simple query
                # protocol handles multi-statement scripts and dollar-quoted
                # function bodies; a parameterised execute would not.
                await connection.execute(sql)
            except asyncpg.PostgresError as exc:
                print(f"\n{name} failed:\n  {exc}", file=sys.stderr)
                return 1

            await connection.execute(
                "INSERT INTO schema_migrations (filename, checksum) VALUES ($1, $2)",
                name,
                digest,
            )
            print("    done")

        if changed:
            # Exit 1, so the compose `migrate` step fails and the API does not
            # boot against a schema that no longer matches its files. It used
            # to print the warning above and report success.
            print(f"\n{changed} applied migration(s) changed on disk.", file=sys.stderr)
            return 1
        if status_only:
            print(f"\n{pending} pending, {len(applied)} applied")
        elif pending == 0:
            print("\nDatabase is up to date")
        else:
            print(f"\nApplied {pending} migration(s)")
        return 0
    finally:
        await connection.close()


def _safe_dsn(dsn: str) -> str:
    """Hide the password when echoing a DSN into an error message."""
    return re.sub(r"://([^:]+):[^@]*@", r"://\1:***@", dsn)


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply SQL migrations")
    parser.add_argument(
        "--status", action="store_true", help="show pending migrations without applying"
    )
    args = parser.parse_args()
    return asyncio.run(run(status_only=args.status))


if __name__ == "__main__":
    raise SystemExit(main())
