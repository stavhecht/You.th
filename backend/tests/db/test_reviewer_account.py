"""The reviewer's own password: changing it, and resetting a forgotten one (needs a database).

A reviewer token carries a fingerprint of the password hash, as a reader's
does, so changing or resetting the password signs out every other session.
A forgotten password is reset from an emailed link, never by knowing the old
one. Rolled back after each test.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import jwt
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.console import auth as console_auth
from app.api.deps import get_login_throttle, get_reviewer_reset_throttle, get_session
from app.config import get_settings
from app.main import create_app
from app.security.auth import AuthError, decode_token_subject, hash_password
from app.security.login_throttle import InMemoryLoginThrottle

DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or get_settings().database_url
PASSWORD = "A-sufficiently-long-passw0rd!"
NEW_PASSWORD = "Another-long-enough-passw0rd!"


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


@pytest.fixture
def mailbox(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Outgoing mail, captured and never sent."""
    sent: list[Any] = []

    async def _capture(message: Any, *_: Any) -> None:
        sent.append(message)

    monkeypatch.setattr(console_auth, "send_email", _capture)
    return sent


@pytest.fixture
async def http(session: AsyncSession, mailbox: list[Any]) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()

    async def _session() -> Any:
        yield session

    app.dependency_overrides[get_session] = _session
    throttle = InMemoryLoginThrottle()
    app.dependency_overrides[get_login_throttle] = lambda: throttle
    app.dependency_overrides[get_reviewer_reset_throttle] = InMemoryLoginThrottle
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
async def email(session: AsyncSession) -> str:
    address = f"reviewer-{uuid.uuid4().hex[:8]}@example.com"
    await session.execute(
        sa.text(
            "INSERT INTO users (email, password_hash, display_name, role)"
            " VALUES (:email, :hash, 'Test Reviewer', 'reviewer')"
        ),
        {"email": address, "hash": hash_password(PASSWORD)},
    )
    await session.flush()
    return address


async def _login(http: httpx.AsyncClient, email: str, password: str) -> httpx.Response:
    return await http.post(
        "/api/console/auth/login", json={"email": email, "password": password}
    )


def _bearer(response: httpx.Response) -> dict[str, str]:
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def test_changing_the_password_signs_out_the_old_session(
    http: httpx.AsyncClient, email: str
) -> None:
    old = _bearer(await _login(http, email, PASSWORD))
    assert (await http.get("/api/console/auth/me", headers=old)).status_code == 200

    changed = await http.post(
        "/api/console/auth/password",
        headers=old,
        json={"currentPassword": PASSWORD, "newPassword": NEW_PASSWORD},
    )
    assert changed.status_code == 200

    assert (await http.get("/api/console/auth/me", headers=old)).status_code == 401
    assert (await http.get("/api/console/auth/me", headers=_bearer(changed))).status_code == 200
    assert (await _login(http, email, PASSWORD)).status_code == 401
    assert (await _login(http, email, NEW_PASSWORD)).status_code == 200


async def test_a_wrong_current_password_changes_nothing(
    http: httpx.AsyncClient, email: str
) -> None:
    auth = _bearer(await _login(http, email, PASSWORD))
    refused = await http.post(
        "/api/console/auth/password",
        headers=auth,
        json={"currentPassword": "not-the-password", "newPassword": NEW_PASSWORD},
    )
    assert refused.status_code == 403
    assert (await _login(http, email, PASSWORD)).status_code == 200


async def test_a_short_new_password_is_refused(http: httpx.AsyncClient, email: str) -> None:
    auth = _bearer(await _login(http, email, PASSWORD))
    refused = await http.post(
        "/api/console/auth/password",
        headers=auth,
        json={"currentPassword": PASSWORD, "newPassword": "short"},
    )
    assert refused.status_code == 422


def _reset_token(message: Any) -> str:
    return str(message.get_content()).split("/review/reset?token=")[1].split()[0]


async def test_a_forgotten_password_is_reset_from_the_emailed_link(
    http: httpx.AsyncClient, email: str, mailbox: list[Any]
) -> None:
    old = _bearer(await _login(http, email, PASSWORD))

    asked = await http.post("/api/console/auth/password/forgot", json={"email": email})
    assert asked.status_code == 202
    assert len(mailbox) == 1 and mailbox[0]["To"] == email
    token = _reset_token(mailbox[0])

    reset = await http.post(
        "/api/console/auth/password/reset", json={"token": token, "password": NEW_PASSWORD}
    )
    assert reset.status_code == 204
    assert (await _login(http, email, NEW_PASSWORD)).status_code == 200
    assert (await _login(http, email, PASSWORD)).status_code == 401
    # Every old session is signed out, and the link works once.
    assert (await http.get("/api/console/auth/me", headers=old)).status_code == 401
    again = await http.post(
        "/api/console/auth/password/reset", json={"token": token, "password": PASSWORD}
    )
    assert again.status_code == 400


async def test_forgot_answers_the_same_for_an_unknown_address(
    http: httpx.AsyncClient, mailbox: list[Any]
) -> None:
    """Or the form would tell anyone which addresses are reviewers."""
    asked = await http.post(
        "/api/console/auth/password/forgot", json={"email": "nobody@example.com"}
    )
    assert asked.status_code == 202
    assert mailbox == []


async def test_a_reader_reset_link_cannot_reset_a_reviewer(
    http: httpx.AsyncClient, email: str, session: AsyncSession
) -> None:
    """The two surfaces' links are not interchangeable, even for the same id."""
    from app.domain.models import User
    from app.security.auth import create_reset_token

    user = (
        await session.execute(sa.select(User).where(User.email == email))
    ).scalar_one()
    reader_link = create_reset_token(user.id, user.password_hash, get_settings().jwt_secret)
    refused = await http.post(
        "/api/console/auth/password/reset",
        json={"token": reader_link, "password": NEW_PASSWORD},
    )
    assert refused.status_code == 400


def test_a_token_that_does_not_say_which_surface_it_is_for_is_refused() -> None:
    """It used to default to `reviewer`, the more privileged reading."""
    secret = "a-test-secret-that-is-at-least-32-chars"
    token = jwt.encode({"sub": "someone", "exp": int(time.time()) + 60}, secret, "HS256")
    with pytest.raises(AuthError):
        decode_token_subject(token, secret)
