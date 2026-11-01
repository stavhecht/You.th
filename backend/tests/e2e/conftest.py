"""The end-to-end suite's database, app client and pipeline runner.

Runs against a **separate** database (``evidwell_test`` by default, or
``E2E_DATABASE_URL``), never the dev one, for two reasons:

* every test starts from empty tables, so assertions can say "the feed holds
  exactly this article" — which a database of real articles cannot;
* runs queued here are invisible to the docker worker, which polls the dev
  database and would otherwise claim a test run and generate it with the
  real model.

Create it once with ``CREATE DATABASE evidwell_test`` and
``DATABASE_URL=…/evidwell_test python -m scripts.migrate``. Skipped when absent.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Callable
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.pipeline.runner as runner
from app.api import deps
from app.main import create_app
from app.pipeline.runner import PipelineWorker
from app.security.auth import hash_password
from app.security.login_throttle import InMemoryLoginThrottle
from tests.e2e.support import (
    E2E_URL,
    FakeExtraction,
    FakeProvider,
    ScriptedWriter,
    build_stages,
    paper,
)

REVIEWER_EMAIL = "reviewer@e2e.test"
REVIEWER_PASSWORD = "Reviewer-password-1"


@pytest.fixture
async def factory() -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    # NullPool: TestClient runs the app on its own event loop, and an asyncpg
    # connection pooled on one loop fails when the other picks it up.
    engine = create_async_engine(E2E_URL, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            name = await connection.scalar(sa.text("SELECT current_database()"))
    except Exception:
        await engine.dispose()
        pytest.skip(f"no end-to-end database at {E2E_URL} (see tests/e2e/conftest.py)")
    # The guard that keeps this from ever emptying the dev database.
    assert name.endswith("_test"), f"refusing to truncate {name!r}: not a *_test database"

    async with engine.begin() as connection:
        tables = (
            (
                await connection.execute(
                    sa.text(
                        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                        "AND tablename <> 'schema_migrations'"
                    )
                )
            )
            .scalars()
            .all()
        )
        await connection.execute(
            sa.text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE")
        )
    yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    await engine.dispose()


@pytest.fixture
def client(factory: async_sessionmaker[AsyncSession]) -> Any:
    """The real app on the test database, with fresh throttles per test."""
    app = create_app()

    async def session() -> AsyncGenerator[AsyncSession]:
        async with factory() as db:
            try:
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    app.dependency_overrides[deps.get_session] = session
    for getter in (
        deps.get_login_throttle,
        deps.get_reader_login_throttle,
        deps.get_contact_throttle,
        deps.get_signup_throttle,
    ):
        throttle = InMemoryLoginThrottle()
        app.dependency_overrides[getter] = lambda t=throttle: t
    with TestClient(app) as http:
        yield http


@pytest.fixture
async def reviewer(factory: async_sessionmaker[AsyncSession]) -> str:
    user_id = str(uuid.uuid4())
    async with factory() as db:
        await db.execute(
            sa.text(
                "INSERT INTO users (id, email, password_hash, display_name, role, created_at)"
                " VALUES (:id, :email, :hash, 'E2E Reviewer', 'reviewer', now())"
            ),
            {"id": user_id, "email": REVIEWER_EMAIL, "hash": hash_password(REVIEWER_PASSWORD)},
        )
        await db.commit()
    return user_id


@pytest.fixture
def console(client: Any, reviewer: str) -> dict[str, str]:
    """Bearer headers for the reviewer, obtained through the real login route."""
    response = client.post(
        "/api/console/auth/login", json={"email": REVIEWER_EMAIL, "password": REVIEWER_PASSWORD}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


RunWorker = Callable[..., Any]


@pytest.fixture
def run_worker(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> RunWorker:
    """One pass of the real worker over the test database: claim the oldest
    queued run, drive the orchestrator through all nine stages, stop.

    Only the stage *dependencies* are swapped (``build_default_pipeline``);
    the claim, heartbeat, orchestrator and transaction boundaries are real.
    """

    async def _run(
        *,
        provider: FakeProvider | None = None,
        writer: ScriptedWriter | None = None,
        extraction: FakeExtraction | None = None,
    ) -> None:
        provider = provider or FakeProvider([paper(n) for n in range(1, 4)])
        monkeypatch.setattr(runner, "get_session_factory", lambda: factory)
        monkeypatch.setattr(
            runner,
            "build_default_pipeline",
            lambda session, _settings: build_stages(
                session, provider=provider, writer=writer, extraction=extraction
            ),
        )
        worker = PipelineWorker(poll_interval=0.01, sweep_interval=3600)
        claim = worker._claim_next_run

        async def claim_once() -> Any:
            worker.request_shutdown()  # finish this run, then stop
            return await claim()

        worker._claim_next_run = claim_once  # type: ignore[method-assign]
        await worker.run_forever()

    return _run
