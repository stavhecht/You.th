"""A cursor whose id is not a UUID is a malformed cursor, not a server error.

Cursors are opaque, but they come back from the client. An id that is not a
UUID used to reach Postgres, which refused it, and the API answered 500 with a
stack trace in the log — on the public feed, for anyone who edited the URL.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_session, require_reviewer
from app.main import create_app
from app.services import feed, review


def _b64(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode()).decode()


def test_the_feed_cursor_refuses_an_id_that_is_not_a_uuid() -> None:
    with pytest.raises(ValueError):
        feed._decode_cursor(_b64("0|2026-01-01T00:00:00+00:00|x"))


def test_the_review_queue_cursor_refuses_an_id_that_is_not_a_uuid() -> None:
    with pytest.raises(review.ReviewError):
        review._decode_cursor(_b64("2026-01-01T00:00:00+00:00|x"))


def test_good_cursors_still_round_trip() -> None:
    article_id = "00000000-0000-0000-0000-000000000001"
    when = "2026-01-01T00:00:00+00:00"
    assert feed._decode_cursor(_b64(f"0|{when}|{article_id}"))[2] == article_id
    assert review._decode_cursor(_b64(f"{when}|{article_id}"))[1] == article_id


def test_the_run_list_answers_400_for_a_cursor_that_is_not_a_uuid() -> None:
    app = create_app()

    async def _never(*_: Any) -> Any:
        raise AssertionError("queried the database with a malformed cursor")

    async def _session() -> Any:
        yield SimpleNamespace(get=_never)

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[require_reviewer] = lambda: SimpleNamespace(id="r")

    with TestClient(app) as http:
        response = http.get("/api/console/pipeline/runs", params={"cursor": "x"})

    assert response.status_code == 400
