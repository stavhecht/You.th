"""Signup is throttled before it hashes, and hashes off the event loop.

Signup runs Argon2 (~33 ms) on every call. Unthrottled, or run on the loop,
that is the CPU amplifier ``login_throttle.py`` describes: a few dozen junk
signups a second stall every other request, the public feed included.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.api.public.readers as readers_api
import app.services.reader as reader_service
from app.api.deps import client_ip, get_session, get_signup_throttle
from app.main import create_app
from app.security.login_throttle import IP_BUDGET, InMemoryLoginThrottle

SIGNUP = "/api/readers/signup"
PASSWORD = "Correct-horse-9"


def _body(n: int) -> dict[str, Any]:
    return {"email": f"reader{n}@example.com", "password": PASSWORD, "displayName": "R"}


def test_signups_past_the_ip_budget_are_refused_before_hashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class _Service:
        def __init__(self, _session: Any) -> None:
            pass

        async def signup(self, *, email: str, **_: Any) -> Any:
            calls.append(email)
            return SimpleNamespace(id="00000000-0000-0000-0000-000000000001")

    monkeypatch.setattr(readers_api, "ReaderService", _Service)
    app = create_app()

    async def _session() -> Any:
        yield SimpleNamespace()

    app.dependency_overrides[get_session] = _session
    throttle = InMemoryLoginThrottle()
    app.dependency_overrides[get_signup_throttle] = lambda: throttle
    app.dependency_overrides[client_ip] = lambda: "10.0.0.9"

    with TestClient(app) as http:
        # Different addresses, one IP: the IP budget is what has to hold.
        allowed = [http.post(SIGNUP, json=_body(n)) for n in range(IP_BUDGET.max_failures)]
        refused = http.post(SIGNUP, json=_body(999))

    assert [r.status_code for r in allowed] == [201] * IP_BUDGET.max_failures
    assert refused.status_code == 429
    assert "Retry-After" in refused.headers
    assert len(calls) == IP_BUDGET.max_failures  # the refused one never reached the hash


async def test_signup_hashes_the_password_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop_thread = threading.get_ident()
    hashed_on: list[int] = []

    def _hash(password: str) -> str:
        hashed_on.append(threading.get_ident())
        return "hash"

    class _Session:
        def add(self, _row: Any) -> None:
            pass

        def begin_nested(self) -> Any:
            class _Nested:
                async def __aenter__(self) -> None:
                    return None

                async def __aexit__(self, *_: Any) -> None:
                    return None

            return _Nested()

        async def flush(self) -> None:
            pass

    monkeypatch.setattr(reader_service, "hash_password", _hash)
    await reader_service.ReaderService(_Session()).signup(  # type: ignore[arg-type]
        email="a@example.com", password=PASSWORD, display_name="A"
    )

    assert hashed_on and hashed_on[0] != loop_thread


async def test_password_checks_run_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Login and account deletion verify through ``check_password``; it must
    answer as ``verify_password`` does, from another thread."""
    from app.security import auth

    stored = auth.hash_password(PASSWORD)
    real_verify = auth.verify_password
    verified_on: list[int] = []

    def _verify(plaintext: str, hashed: str) -> bool:
        verified_on.append(threading.get_ident())
        return real_verify(plaintext, hashed)

    monkeypatch.setattr(auth, "verify_password", _verify)

    assert await auth.check_password(PASSWORD, stored) is True
    assert await auth.check_password("wrong-password", stored) is False
    await auth.equalise_timing()
    assert len(verified_on) == 3
    assert threading.get_ident() not in verified_on


@pytest.mark.parametrize(
    ("path", "body", "override"),
    [
        (
            "/api/readers/login",
            {"email": "a@example.com", "password": "x"},
            "get_reader_login_throttle",
        ),
        ("/api/contact", {"email": "a@example.com", "note": "hello"}, "get_contact_throttle"),
    ],
)
def test_a_blocked_caller_gets_a_429_with_retry_after(
    path: str, body: dict[str, Any], override: str
) -> None:
    """The routes ``too_many_attempts`` serves that had no 429 test of their own."""
    import app.api.deps as deps

    class _Blocked:
        def retry_after(self, **_: Any) -> float:
            return 0.4  # under a second still says "1", never "0"

    app = create_app()

    async def _session() -> Any:
        yield SimpleNamespace()

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[getattr(deps, override)] = lambda: _Blocked()

    with TestClient(app) as http:
        response = http.post(path, json=body)

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "1"
