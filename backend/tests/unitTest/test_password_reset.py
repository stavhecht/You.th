"""Forgot-your-password: a signed, single-use, 30-minute link, and no account oracle."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import jwt
import pytest

import app.api.deps as deps
import app.api.public.readers as readers_api
from app.main import create_app
from app.security.auth import (
    JWT_ALGORITHM,
    TOKEN_TYPE_READER,
    AuthError,
    create_reader_token,
    create_reset_token,
    decode_token_subject,
    password_unchanged,
    reset_token_subject,
)
from app.security.login_throttle import InMemoryLoginThrottle
from app.services.reader import ReaderError

SECRET = "test-secret-at-least-32-characters-long!"
READER_ID = "11111111-1111-1111-1111-111111111111"


def test_the_link_names_its_reader_and_dies_when_the_password_changes() -> None:
    token = create_reset_token(READER_ID, "old-hash", SECRET)
    subject, fingerprint = reset_token_subject(token, SECRET)
    assert subject == READER_ID
    assert password_unchanged(fingerprint, "old-hash")
    assert not password_unchanged(fingerprint, "new-hash")


def test_a_reset_link_is_not_a_session_and_a_session_is_not_a_reset_link() -> None:
    token = create_reset_token(READER_ID, "hash", SECRET)
    with pytest.raises(AuthError):
        decode_token_subject(token, SECRET, expect=TOKEN_TYPE_READER)
    with pytest.raises(AuthError):
        decode_token_subject(token, SECRET)  # the console's
    session = jwt.encode(
        {
            "sub": READER_ID,
            "typ": TOKEN_TYPE_READER,
            "exp": datetime.now(UTC) + timedelta(days=1),
        },
        SECRET,
        algorithm=JWT_ALGORITHM,
    )
    with pytest.raises(AuthError):
        reset_token_subject(session, SECRET)


def test_an_expired_link_is_refused() -> None:
    expired = jwt.encode(
        {
            "sub": READER_ID,
            "typ": "reader_reset",
            "pwd": "x",
            "exp": datetime.now(UTC) - timedelta(seconds=1),
        },
        SECRET,
        algorithm=JWT_ALGORITHM,
    )
    with pytest.raises(AuthError):
        reset_token_subject(expired, SECRET)


class _Session:
    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The app with a fake session, no throttle, a fixed secret, and a mail outbox."""
    from app.config import get_settings

    state: dict[str, Any] = {"sent": [], "password_hash": "current-hash", "set": None}

    async def _session() -> Any:
        yield _Session()

    async def _by_email(self: Any, email: str) -> Any:
        if email != "ada@example.com":
            return None
        return SimpleNamespace(id=READER_ID, email=email, password_hash=state["password_hash"])

    async def _get(self: Any, reader_id: str) -> Any:
        if reader_id != READER_ID:
            raise ReaderError("no such reader")
        return SimpleNamespace(id=READER_ID, password_hash=state["password_hash"])

    async def _set_password(self: Any, reader: Any, password: str) -> None:
        state["set"] = password
        state["password_hash"] = "changed-hash"

    async def _send(message: Any, settings: Any, what: str) -> None:
        state["sent"].append(message)

    monkeypatch.setattr(readers_api.ReaderService, "by_email", _by_email)
    monkeypatch.setattr(readers_api.ReaderService, "get", _get)
    monkeypatch.setattr(readers_api.ReaderService, "set_password", _set_password)
    monkeypatch.setattr(readers_api, "send_email", _send)

    settings = get_settings().model_copy(update={"jwt_secret": SECRET})
    allow = SimpleNamespace(
        retry_after=lambda **_: None,
        record_failure=lambda **_: None,
        record_success=lambda **_: None,
    )
    app = create_app()
    app.dependency_overrides[deps.get_session] = _session
    app.dependency_overrides[deps.get_settings] = lambda: settings
    app.dependency_overrides[deps.get_password_reset_throttle] = lambda: allow
    app.dependency_overrides[deps.client_ip] = lambda: "203.0.113.7"
    state["app"] = app
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test"), state


async def test_a_known_and_an_unknown_address_get_the_same_answer(client: Any) -> None:
    http, state = client
    async with http:
        known = await http.post(
            "/api/readers/password/forgot", json={"email": "ada@example.com"}
        )
        unknown = await http.post(
            "/api/readers/password/forgot", json={"email": "nobody@example.com"}
        )
    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()
    assert len(state["sent"]) == 1
    assert state["sent"][0]["To"] == "ada@example.com"
    assert "/forgot-password?token=" in state["sent"][0].get_content()


async def test_the_emailed_link_resets_the_password_once(client: Any) -> None:
    http, state = client
    async with http:
        await http.post("/api/readers/password/forgot", json={"email": "ada@example.com"})
        body = state["sent"][0].get_content()
        token = body.split("token=")[1].split()[0]
        first = await http.post(
            "/api/readers/password/reset", json={"token": token, "password": "New-password-1"}
        )
        again = await http.post(
            "/api/readers/password/reset", json={"token": token, "password": "Other-password-2"}
        )
    assert first.status_code == 204
    assert state["set"] == "New-password-1"
    assert again.status_code == 400


async def test_a_reset_keeps_the_signup_password_rules(client: Any) -> None:
    http, _ = client
    token = create_reset_token(READER_ID, "current-hash", SECRET)
    async with http:
        weak = await http.post(
            "/api/readers/password/reset", json={"token": token, "password": "password"}
        )
    assert weak.status_code == 422


async def test_a_forged_link_is_refused(client: Any) -> None:
    http, state = client
    forged = create_reset_token(
        READER_ID, "current-hash", "another-secret-of-32-characters-plus"
    )
    async with http:
        response = await http.post(
            "/api/readers/password/reset", json={"token": forged, "password": "New-password-1"}
        )
    assert response.status_code == 400
    assert state["set"] is None


async def test_bad_reset_links_cannot_lock_everyone_out(client: Any) -> None:
    """A throttle keyed on no email made every reset on the site share one counter."""
    http, state = client
    state["app"].dependency_overrides[deps.get_password_reset_throttle] = InMemoryLoginThrottle
    good = create_reset_token(READER_ID, "current-hash", SECRET)
    async with http:
        junk = [
            await http.post(
                "/api/readers/password/reset", json={"token": "x", "password": "New-password-1"}
            )
            for _ in range(30)
        ]
        real = await http.post(
            "/api/readers/password/reset", json={"token": good, "password": "New-password-1"}
        )
    assert {response.status_code for response in junk} == {400}
    assert real.status_code == 204


async def test_a_reset_signs_out_every_device() -> None:
    """A session token issued before the password changed stops working."""
    from fastapi.security import HTTPAuthorizationCredentials

    reader = SimpleNamespace(
        id=READER_ID, email="ada@example.com", display_name="Ada", password_hash="old-hash"
    )

    class _Result:
        def scalar_one_or_none(self) -> Any:
            return reader

    class _Lookup:
        async def execute(self, *_: Any) -> _Result:
            return _Result()

    settings: Any = SimpleNamespace(jwt_secret=SECRET)

    async def resolve(token: str) -> Any:
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        return await deps._resolve_reader(_Lookup(), settings, credentials)  # type: ignore[arg-type]

    token, _ = create_reader_token(READER_ID, SECRET, "old-hash")
    legacy = jwt.encode(
        {
            "sub": READER_ID,
            "typ": TOKEN_TYPE_READER,
            "exp": datetime.now(UTC) + timedelta(days=1),
        },
        SECRET,
        algorithm=JWT_ALGORITHM,
    )
    assert (await resolve(token)).id == READER_ID
    reader.password_hash = "new-hash"
    assert await resolve(token) is None
    assert (
        await resolve(legacy)
    ).id == READER_ID  # issued before fingerprints, until it expires


async def test_one_address_gets_at_most_three_reset_emails_an_hour(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each request mails a stranger; the login's budget let one address get twenty."""
    http, state = client
    configured = deps._password_reset_throttle._budgets["account"]  # type: ignore[attr-defined]
    fresh = InMemoryLoginThrottle(account_budget=configured)
    state["app"].dependency_overrides[deps.get_password_reset_throttle] = lambda: fresh
    ips = iter(f"203.0.113.{n}" for n in range(1, 100))  # a new IP each time
    state["app"].dependency_overrides[deps.client_ip] = lambda: next(ips)
    async with http:
        answers = [
            (
                await http.post(
                    "/api/readers/password/forgot", json={"email": "ada@example.com"}
                )
            ).status_code
            for _ in range(6)
        ]
    assert answers == [202, 202, 202, 429, 429, 429]
    assert len(state["sent"]) == 3
