"""Promoting a trend candidate locks its row (needs ``youth_test``).

Unlocked, two presses of Generate draft both read ``proposed`` and both queued a
run: the same article generated twice. The rows have to be committed for two
sessions to see them, and a committed queued run in the dev database is claimed
by the docker worker within five seconds, so this runs on ``youth_test`` like
``test_social_db.py``. Skipped when that database is absent.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.api.console.discovery import promote_candidate
from app.api.console.research import promote_research_candidate
from app.config import get_settings
from app.domain.enums import UserRole
from app.security.auth import AuthenticatedReviewer

URL = os.environ.get("SOCIAL_TEST_DATABASE_URL") or make_url(get_settings().database_url).set(
    database="youth_test"
)
UI = "D999909"  # outside the range MeSH assigns, like test_discovery_db's


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(URL, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.execute(sa.text("SELECT 1 FROM research_candidates LIMIT 1"))
    except Exception:
        await engine.dispose()
        pytest.skip(f"no migrated test database at {URL}")
    yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    await engine.dispose()


@pytest.fixture
async def reviewer(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AuthenticatedReviewer]:
    user_id = str(uuid.uuid4())
    email = f"promote-{user_id[:8]}@example.com"
    async with factory() as db:
        await db.execute(
            sa.text(
                "INSERT INTO users (id, email, password_hash, display_name, role)"
                " VALUES (:id, :email, 'x', 'Promote Test', 'reviewer')"
            ),
            {"id": user_id, "email": email},
        )
        await db.commit()
    yield AuthenticatedReviewer(
        id=user_id, email=email, display_name="Promote Test", role=UserRole.REVIEWER
    )
    async with factory() as db:
        # Candidates first: deleting the user SET-NULLs decided_by, which the
        # decided-has-reviewer CHECK refuses.
        for table in ("discovery_candidates", "research_candidates"):
            await db.execute(
                sa.text(f"DELETE FROM {table} WHERE decided_by = :id"),
                {"id": user_id},
            )
        await db.execute(
            sa.text("DELETE FROM pipeline_runs WHERE requested_by = :id"), {"id": user_id}
        )
        await db.execute(sa.text("DELETE FROM research_runs WHERE label LIKE 'promote_test_%'"))
        await db.execute(sa.text("DELETE FROM users WHERE id = :id"), {"id": user_id})
        await db.commit()


async def _discovery_candidate(db: AsyncSession) -> str:
    candidate_id = str(uuid.uuid4())
    await db.execute(
        sa.text(
            "INSERT INTO discovery_descriptors (ui, name, kind)"
            " VALUES (:ui, 'Promote test', 'substance') ON CONFLICT (ui) DO NOTHING"
        ),
        {"ui": UI},
    )
    await db.execute(
        sa.text(
            "INSERT INTO discovery_candidates"
            " (id, substance_ui, topic, score, paper_count, baseline_count, rationale)"
            " VALUES (:id, :ui, 'promote lock test', 1, 3, 1, '{}')"
        ),
        {"id": candidate_id, "ui": UI},
    )
    return candidate_id


async def _research_candidate(db: AsyncSession) -> str:
    run_id, candidate_id = str(uuid.uuid4()), str(uuid.uuid4())
    await db.execute(
        sa.text(
            "INSERT INTO research_runs (id, label, mode, status, params, config)"
            " VALUES (:id, :label, 'manual', 'completed', '{}'::jsonb, '{}'::jsonb)"
        ),
        {"id": run_id, "label": f"promote_test_{run_id[:8]}"},
    )
    await db.execute(
        sa.text(
            "INSERT INTO research_candidates"
            " (id, research_run_id, canonical_topic, queries, signals, scores, status)"
            " VALUES (:id, :run, 'promote lock test', '[]'::jsonb, '{}'::jsonb,"
            "  '{}'::jsonb, 'selected')"
        ),
        {"id": candidate_id, "run": run_id},
    )
    return candidate_id


@pytest.mark.parametrize(
    ("make", "promote"),
    [
        (_discovery_candidate, promote_candidate),
        (_research_candidate, promote_research_candidate),
    ],
    ids=["discovery", "research"],
)
async def test_two_presses_queue_one_run(
    factory: async_sessionmaker[AsyncSession],
    reviewer: AuthenticatedReviewer,
    make: Callable[[AsyncSession], Awaitable[str]],
    promote: Callable[..., Awaitable[Any]],
) -> None:
    async with factory() as db:
        candidate_id = await make(db)
        await db.commit()

    async with factory() as first, factory() as second:
        await promote(candidate_id, first, reviewer)  # not committed yet
        late = asyncio.create_task(promote(candidate_id, second, reviewer))
        await asyncio.sleep(0.3)  # let the second press reach the row and wait on it
        await first.commit()

        with pytest.raises(HTTPException) as caught:
            await late
        assert caught.value.status_code == 409
        await second.rollback()

    async with factory() as db:
        runs = await db.scalar(
            sa.text(
                "SELECT count(*) FROM pipeline_runs"
                " WHERE requested_by = :id AND topic = 'promote lock test'"
            ),
            {"id": reviewer.id},
        )
    assert runs == 1
