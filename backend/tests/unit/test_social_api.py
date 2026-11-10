"""The collection triggers — desk button and n8n — reach the same service call.

``start_collection_run`` is replaced with a recorder: this checks routing,
auth and serialization; ``test_social_db.py`` checks the database.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_session, require_reviewer
from app.config import Settings, get_settings
from app.domain.enums import UserRole
from app.domain.models import CollectionRun
from app.main import create_app
from app.security.auth import AuthenticatedReviewer
from app.social import service
from app.social.contracts import CollectionKind

TOKEN = "t" * 40
REVIEWER = AuthenticatedReviewer(
    id="11111111-1111-1111-1111-111111111111",
    email="reviewer@example.com",
    display_name="Reviewer",
    role=UserRole.REVIEWER,
)


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[CollectionKind] = []
        self.active = False

    async def __call__(
        self, session: Any, kind: CollectionKind
    ) -> CollectionRun:
        if self.active:
            raise service.CollectionAlreadyActive("social_daily_x is already queued or running")
        self.calls.append(kind)
        return CollectionRun(
            id="22222222-2222-2222-2222-222222222222",
            label=f"{kind.value}_2026_11_04_abcd",
            kind=kind.value,
            status="queued",
            collection_status={},
            stats={},
            notes=[],
            error=None,
            created_at=datetime(2026, 11, 4, tzinfo=UTC),
            started_at=None,
            finished_at=None,
        )


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    fake = _Recorder()
    monkeypatch.setattr(service, "start_collection_run", fake)
    return fake


def _client(token: str) -> Iterator[TestClient]:
    app = create_app()

    async def _session() -> Any:
        yield object()

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[require_reviewer] = lambda: REVIEWER
    app.dependency_overrides[get_settings] = lambda: Settings(
        research_trigger_token=token,
        discovery_scan_enabled=False,
        collection_schedule_enabled=False,
    )
    with TestClient(app) as http:
        yield http


@pytest.fixture
def http() -> Iterator[TestClient]:
    yield from _client(TOKEN)


def test_the_desk_and_n8n_queue_the_same_run(http: TestClient, recorder: _Recorder) -> None:
    desk = http.post("/api/console/collection/runs", json={"kind": "social_weekly"})
    n8n = http.post(
        "/api/automation/collection/runs",
        json={"kind": "product_discovery"},
        headers={"X-Trigger-Token": TOKEN},
    )
    assert desk.status_code == n8n.status_code == 202
    assert recorder.calls == [CollectionKind.SOCIAL_WEEKLY, CollectionKind.PRODUCT_DISCOVERY]
    body = desk.json()
    assert body["kind"] == "social_weekly" and "collectionStatus" in body


def test_a_second_run_of_a_kind_is_a_409(http: TestClient, recorder: _Recorder) -> None:
    recorder.active = True
    response = http.post("/api/console/collection/runs", json={"kind": "social_daily"})
    assert response.status_code == 409


def test_an_unknown_kind_is_refused(http: TestClient, recorder: _Recorder) -> None:
    response = http.post("/api/console/collection/runs", json={"kind": "tiktok_scrape"})
    assert response.status_code == 422 and recorder.calls == []


def test_n8n_needs_its_token(http: TestClient, recorder: _Recorder) -> None:
    response = http.post("/api/automation/collection/runs", json={"kind": "social_daily"})
    assert response.status_code == 401 and recorder.calls == []


def test_without_a_token_configured_the_automation_route_does_not_exist(
    recorder: _Recorder,
) -> None:
    for client in _client(""):
        response = client.post(
            "/api/automation/collection/runs",
            json={"kind": "social_daily"},
            headers={"X-Trigger-Token": TOKEN},
        )
        assert response.status_code == 404


def test_a_malformed_id_is_a_422_not_a_500(http: TestClient) -> None:
    """Thirty-six dashes have a UUID's length and characters, and the database refuses them."""
    response = http.get(f"/api/console/intel/related?kind=product&id={'-' * 36}")
    assert response.status_code == 422
