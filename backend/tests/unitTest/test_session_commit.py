"""The request session commits before the response is sent.

FastAPI runs a ``yield`` dependency's exit code *after* the response by default,
so a commit that failed there still answered 2xx — a reviewer saw "Published"
for an article that was not. ``get_session`` is therefore function-scoped.
This drives the real dependency (no override) with a session whose commit
raises, so it fails if the scope is ever dropped.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.api.deps as deps
from app.main import create_app


class _CommitFails:
    def __init__(self) -> None:
        self.rolled_back = False

    async def __aenter__(self) -> _CommitFails:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def execute(self, *_: Any, **__: Any) -> None:
        return None

    async def commit(self) -> None:
        raise RuntimeError("commit failed")

    async def rollback(self) -> None:
        self.rolled_back = True


def test_a_failed_commit_is_not_reported_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _CommitFails()
    monkeypatch.setattr(deps, "get_session_factory", lambda: lambda: session)

    with TestClient(create_app(), raise_server_exceptions=False) as http:
        response = http.get("/api/healthz")

    assert response.status_code == 500
    assert session.rolled_back
