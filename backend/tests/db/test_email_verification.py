"""A reader proves an email address is theirs before acting under it (needs a database).

Signup takes any address. Until the link mailed to it is opened, the account
works for reading and saving but cannot send the editors a topic suggestion
"from" that address. Rolled back after each test.
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

from app.api import deps
from app.api.public import contact as contact_api
from app.api.public import readers as readers_api
from app.config import get_settings
from app.main import create_app
from app.security.auth import create_verify_token
from app.security.login_throttle import InMemoryLoginThrottle

DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or get_settings().database_url
PASSWORD = "A-sufficiently-long-passw0rd!"


@pytest.fixture
async def session() -> Any:
    engine = create_async_engine(DATABASE_URL)
    try:
        async with engine.connect() as connection:
            await connection.execute(sa.text("SELECT email_verified_at FROM readers LIMIT 1"))
    except Exception:
        await engine.dispose()
        pytest.skip(f"no database with migration 0010 at {DATABASE_URL}")
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as db:
        try:
            yield db
        finally:
            await db.rollback()
    await engine.dispose()


@pytest.fixture
async def http(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[httpx.AsyncClient, list[str]]]:
    """The API on the test session, with outgoing mail captured, not sent."""
    sent: list[str] = []

    async def _capture(message: Any, *_: Any) -> None:
        sent.append(message.get_content())

    monkeypatch.setattr(readers_api, "send_email", _capture)
    monkeypatch.setattr(contact_api, "notify_editors", _capture)
    app = create_app()

    async def _session() -> Any:
        yield session

    app.dependency_overrides[deps.get_session] = _session
    for name in ("get_signup_throttle", "get_contact_throttle", "get_password_reset_throttle"):
        app.dependency_overrides[getattr(deps, name)] = InMemoryLoginThrottle
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, sent


async def _signup(client: httpx.AsyncClient) -> tuple[str, dict[str, str]]:
    email = f"reader-{uuid.uuid4().hex[:8]}@example.com"
    response = await client.post(
        "/api/readers/signup",
        json={"email": email, "password": PASSWORD, "displayName": "Ada"},
    )
    assert response.status_code == 201
    return email, {"Authorization": f"Bearer {response.json()['access_token']}"}


def _token_in(mail: str) -> str:
    return mail.split("/verify?token=")[1].split()[0]


TOPIC = {"kind": "topic", "name": "x", "email": "x@example.com", "note": "creatine"}


async def test_signup_mails_a_link_and_the_link_confirms_the_address(
    http: tuple[httpx.AsyncClient, list[str]],
) -> None:
    client, sent = http
    _, auth = await _signup(client)
    assert (await client.get("/api/readers/me", headers=auth)).json()["emailVerified"] is False
    assert len(sent) == 1

    confirmed = await client.post("/api/readers/verify", json={"token": _token_in(sent[0])})

    assert confirmed.status_code == 204
    assert (await client.get("/api/readers/me", headers=auth)).json()["emailVerified"] is True


async def test_an_unconfirmed_reader_cannot_suggest_a_topic(
    http: tuple[httpx.AsyncClient, list[str]],
) -> None:
    client, sent = http
    _, auth = await _signup(client)

    refused = await client.post("/api/contact", json=TOPIC, headers=auth)
    assert refused.status_code == 403

    await client.post("/api/readers/verify", json={"token": _token_in(sent[0])})
    assert (await client.post("/api/contact", json=TOPIC, headers=auth)).status_code == 202


async def test_a_link_made_for_another_address_or_made_up_is_refused(
    http: tuple[httpx.AsyncClient, list[str]], session: AsyncSession
) -> None:
    client, _ = http
    email, auth = await _signup(client)
    reader_id = (await client.get("/api/readers/me", headers=auth)).json()["id"]
    secret = get_settings().jwt_secret

    stale = create_verify_token(reader_id, "an-older-address@example.com", secret)
    assert (await client.post("/api/readers/verify", json={"token": stale})).status_code == 400
    assert (await client.post("/api/readers/verify", json={"token": "junk"})).status_code == 400
    # A session token is not a verification link.
    session_token = auth["Authorization"].removeprefix("Bearer ")
    assert (
        await client.post("/api/readers/verify", json={"token": session_token})
    ).status_code == 400
    assert (await client.get("/api/readers/me", headers=auth)).json()["emailVerified"] is False
    assert email  # the right address was never confirmed by any of them


async def test_the_link_can_be_sent_again(http: tuple[httpx.AsyncClient, list[str]]) -> None:
    client, sent = http
    _, auth = await _signup(client)

    again = await client.post("/api/readers/verify/resend", headers=auth)

    assert again.status_code == 202
    assert len(sent) == 2
    await client.post("/api/readers/verify", json={"token": _token_in(sent[1])})
    assert (await client.get("/api/readers/me", headers=auth)).json()["emailVerified"] is True
