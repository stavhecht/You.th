"""Every console route refuses a caller with no token, and so does n8n's.

Auth is attached per router, which cannot be forgotten per handler — but a new
router included under ``/api/console`` without the dependency would expose its
routes while every behavioural test still passed. So this asks the real app:
every path and method in its OpenAPI schema, called with no credentials. The
auth dependency answers before body validation and before any query, so dummy
path values and empty bodies are enough.
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import create_app

#: Reachable without a token by design: you cannot log in after logging in.
PUBLIC_CONSOLE_PATHS = {"/api/console/auth/login"}
DUMMY_ID = "00000000-0000-0000-0000-000000000000"


def _operations(http: TestClient, prefix: str) -> list[tuple[str, str]]:
    schema = http.get("/openapi.json").json()
    return [
        (method.upper(), re.sub(r"\{[^}]+\}", DUMMY_ID, path))
        for path, item in schema["paths"].items()
        if path.startswith(prefix) and path not in PUBLIC_CONSOLE_PATHS
        for method in item
    ]


def test_every_console_route_refuses_a_caller_with_no_token() -> None:
    with TestClient(create_app()) as http:
        operations = _operations(http, "/api/console")
        assert len(operations) > 20  # the walk found the console at all
        answered = {f"{m} {p}": http.request(m, p).status_code for m, p in operations}

    assert {op: code for op, code in answered.items() if code != 401} == {}


def test_every_automation_route_refuses_a_caller_with_no_token() -> None:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(research_trigger_token="t" * 40)
    with TestClient(app) as http:
        operations = _operations(http, "/api/automation")
        assert operations
        answered = {f"{m} {p}": http.request(m, p).status_code for m, p in operations}

    assert {op: code for op, code in answered.items() if code != 401} == {}
