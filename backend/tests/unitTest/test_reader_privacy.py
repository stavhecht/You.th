"""A reader's copy of their data, erasure, and the operator's request helper.

Needs a live database for the same reason ``test_reader_accounts.py`` does:
what is under test is which rows a DELETE reaches (the ``ON DELETE CASCADE``
on folders and saves) and that nothing reaches another reader's rows. A fake
session cannot express either. **A skipped run verifies none of this.**

The HTTP tests go through ``httpx.ASGITransport`` rather than ``TestClient``
so the app runs on the test's own event loop and shares its session — which is
rolled back afterwards, like every other DB-backed test here.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.deps import get_reader_login_throttle, get_session
from app.config import get_settings
from app.domain.enums import ContactKind
from app.domain.models import Reader
from app.main import create_app
from app.security.auth import create_reader_token
from app.security.login_throttle import InMemoryLoginThrottle
from app.services.contact import ContactService
from app.services.reader import ReaderService
from scripts.privacy_request import erase, holdings

DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or get_settings().database_url
PASSWORD = "A-sufficiently-long-passw0rd!"


@pytest.fixture
async def session() -> Any:
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


async def _make_reader(db: AsyncSession, email: str | None = None) -> Reader:
    return await ReaderService(db).signup(
        email=email or f"reader-{uuid.uuid4().hex[:8]}@example.com",
        password=PASSWORD,
        display_name="Maya",
    )


async def _count(db: AsyncSession, table: str, reader_id: str) -> int:
    column = "id" if table == "readers" else "reader_id"
    return (
        await db.execute(
            sa.text(f"SELECT count(*) FROM {table} WHERE {column} = :id"), {"id": reader_id}
        )
    ).scalar_one()


@pytest.fixture
async def http(session: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()

    async def _session() -> Any:
        yield session

    app.dependency_overrides[get_session] = _session
    # A fresh throttle per test: the real one is process-wide, and failures
    # counted by one test would lock the next one out.
    throttle = InMemoryLoginThrottle()
    app.dependency_overrides[get_reader_login_throttle] = lambda: throttle

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def _auth(reader: Reader) -> dict[str, str]:
    token, _ = create_reader_token(reader.id, get_settings().jwt_secret)
    return {"Authorization": f"Bearer {token}"}


# --- export ----------------------------------------------------------------


async def test_the_export_carries_every_reader_column(session: AsyncSession) -> None:
    """A column added to ``readers`` later must reach the copy without anyone
    remembering to add it — or this fails. The password hash is the one
    deliberate omission."""
    reader = await _make_reader(session)
    exported = await ReaderService(session).export(reader.id)

    columns = {column.name for column in Reader.__table__.columns}
    assert set(exported["account"]) == columns - {"password_hash"}
    assert exported["account"]["email"] == reader.email
    assert [folder["name"] for folder in exported["folders"]] == ["Saved"]


async def test_the_export_holds_only_the_callers_folders(session: AsyncSession) -> None:
    mine = await _make_reader(session)
    theirs = await _make_reader(session)
    await ReaderService(session).create_folder(theirs.id, "Their private folder")

    exported = await ReaderService(session).export(mine.id)

    assert "Their private folder" not in {f["name"] for f in exported["folders"]}


async def test_the_export_route_is_a_no_store_download(
    session: AsyncSession, http: httpx.AsyncClient
) -> None:
    reader = await _make_reader(session)

    response = await http.get("/api/readers/me/export", headers=_auth(reader))

    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith("attachment")
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["account"]["id"] == reader.id
    assert "password_hash" not in body["account"]


async def test_the_export_route_needs_a_reader(http: httpx.AsyncClient) -> None:
    assert (await http.get("/api/readers/me/export")).status_code == 401


# --- erasure ---------------------------------------------------------------


async def test_deleting_an_account_removes_its_folders_and_saves_and_nothing_else(
    session: AsyncSession,
) -> None:
    gone = await _make_reader(session)
    kept = await _make_reader(session)

    await ReaderService(session).delete_account(gone.id)

    for table in ("readers", "reader_folders", "reader_saves"):
        assert await _count(session, table, gone.id) == 0
    assert await _count(session, "readers", kept.id) == 1
    assert await _count(session, "reader_folders", kept.id) == 1


async def test_the_delete_route_refuses_a_wrong_password_and_keeps_the_account(
    session: AsyncSession, http: httpx.AsyncClient
) -> None:
    reader = await _make_reader(session)

    response = await http.request(
        "DELETE", "/api/readers/me", json={"password": "not-it"}, headers=_auth(reader)
    )

    # 403, not 401: the client treats 401 as "your session is gone" and signs
    # the reader out, which is the wrong response to a mistyped password.
    assert response.status_code == 403
    assert await _count(session, "readers", reader.id) == 1


async def test_the_delete_route_erases_the_callers_account_only(
    session: AsyncSession, http: httpx.AsyncClient
) -> None:
    caller = await _make_reader(session)
    bystander = await _make_reader(session)

    response = await http.request(
        "DELETE", "/api/readers/me", json={"password": PASSWORD}, headers=_auth(caller)
    )

    assert response.status_code == 204
    assert await _count(session, "readers", caller.id) == 0
    assert await _count(session, "readers", bystander.id) == 1


async def test_the_delete_route_is_throttled_like_the_login(
    session: AsyncSession, http: httpx.AsyncClient
) -> None:
    """A stolen token must not turn erasure into an unthrottled password oracle."""
    reader = await _make_reader(session)
    statuses = [
        (
            await http.request(
                "DELETE", "/api/readers/me", json={"password": "guess"}, headers=_auth(reader)
            )
        ).status_code
        for _ in range(12)
    ]

    assert 429 in statuses
    assert await _count(session, "readers", reader.id) == 1


async def test_a_deleted_readers_token_stops_working(
    session: AsyncSession, http: httpx.AsyncClient
) -> None:
    reader = await _make_reader(session)
    headers = _auth(reader)
    await http.request(
        "DELETE", "/api/readers/me", json={"password": PASSWORD}, headers=headers
    )

    assert (await http.get("/api/readers/me", headers=headers)).status_code == 401


# --- the operator's request helper -----------------------------------------


async def test_the_request_helper_finds_and_erases_by_address_case_insensitively(
    session: AsyncSession,
) -> None:
    address = f"Person-{uuid.uuid4().hex[:8]}@Example.com"
    reader = await _make_reader(session, address.lower())
    contacts = ContactService(session)
    await contacts.submit(
        kind=ContactKind.OTHER, email=address, note="mine", name=None, link=None
    )
    await contacts.submit(
        kind=ContactKind.OTHER,
        email=f"other-{uuid.uuid4().hex[:8]}@example.com",
        note="someone else's",
        name=None,
        link=None,
    )
    await session.flush()

    held = await holdings(session, address.upper())
    assert held["reader_account"]["account"]["id"] == reader.id
    assert [c["note"] for c in held["contact_requests"]] == ["mine"]

    deleted = await erase(session, address)

    assert deleted == {"reader_accounts": 1, "contact_requests": 1}
    after = await holdings(session, address)
    assert after["reader_account"] is None and after["contact_requests"] == []
    remaining = (
        await session.execute(
            sa.text("SELECT count(*) FROM contact_requests WHERE note = 'someone else''s'")
        )
    ).scalar_one()
    assert remaining >= 1


# --- headers ---------------------------------------------------------------


async def test_api_responses_carry_the_security_headers() -> None:
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/media/00/does-not-exist.webp")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
