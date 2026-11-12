"""The two triggers — desk button and n8n — reach the same service call.

``start_research_run`` is replaced with a recorder, so this checks the routing
and the auth around it, not the database (``test_research_db.py`` does that).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_session, require_reviewer
from app.config import Settings, get_settings
from app.domain.enums import ResearchRunMode, ResearchRunStatus, UserRole
from app.domain.models import ResearchRun
from app.main import create_app
from app.research import service
from app.research.contracts import Category, ResearchParams
from app.security.auth import AuthenticatedReviewer

TOKEN = "t" * 40
REVIEWER = AuthenticatedReviewer(
    id="11111111-1111-1111-1111-111111111111",
    email="reviewer@example.com",
    display_name="Reviewer",
    role=UserRole.REVIEWER,
)


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.active = False

    async def __call__(
        self,
        session: Any,
        *,
        mode: ResearchRunMode,
        params: ResearchParams,
        requested_by: str | None,
        settings: Settings,
    ) -> ResearchRun:
        if self.active:
            raise service.ResearchAlreadyActive("research run research_x is already queued")
        self.calls.append({"mode": mode, "params": params, "requested_by": requested_by})
        return ResearchRun(
            id="22222222-2222-2222-2222-222222222222",
            label="research_2026_09_30_abcd",
            mode=mode,
            status=ResearchRunStatus.QUEUED,
            stage="queued",
            params=params.model_dump(mode="json", exclude_none=True),
            config={},
            provider_status={},
            stage_log=[],
            notes=[],
            error=None,
            created_at=datetime(2026, 9, 30, tzinfo=UTC),
            started_at=None,
            finished_at=None,
        )


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    fake = _Recorder()
    monkeypatch.setattr(service, "start_research_run", fake)
    return fake


def _client(token: str) -> Iterator[TestClient]:
    app = create_app()

    async def _session() -> Any:
        yield object()

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[require_reviewer] = lambda: REVIEWER
    app.dependency_overrides[get_settings] = lambda: Settings(
        research_trigger_token=token, discovery_scan_enabled=False
    )
    with TestClient(app) as http:
        yield http


@pytest.fixture
def http() -> Iterator[TestClient]:
    yield from _client(TOKEN)


@pytest.fixture
def http_without_token() -> Iterator[TestClient]:
    yield from _client("")


BODY = {
    "targetArticleCount": 5,
    "categories": ["sleep_recovery", "supplements"],
    "trendWindowDays": 7,
}


def test_weekly_and_manual_triggers_make_the_same_call(
    http: TestClient, recorder: _Recorder
) -> None:
    manual = http.post("/api/console/research/runs", json=BODY)
    weekly = http.post(
        "/api/automation/research/runs",
        json={**BODY, "mode": "weekly"},
        headers={"X-Trigger-Token": TOKEN},
    )

    assert manual.status_code == weekly.status_code == 202
    desk, n8n = recorder.calls
    assert desk["params"] == n8n["params"]
    assert desk["params"].categories == [Category.SLEEP_RECOVERY, Category.SUPPLEMENTS]
    # Not just equal to each other: both used to lose every two-word field the
    # same way, which this comparison alone cannot see.
    assert desk["params"].target_article_count == 5
    assert desk["params"].trend_window_days == 7
    assert (desk["mode"], n8n["mode"]) == (ResearchRunMode.MANUAL, ResearchRunMode.WEEKLY)
    assert (desk["requested_by"], n8n["requested_by"]) == (REVIEWER.id, None)
    assert weekly.json()["label"].startswith("research_")


def test_n8n_can_also_start_a_manual_run(http: TestClient, recorder: _Recorder) -> None:
    response = http.post(
        "/api/automation/research/runs",
        json={"mode": "manual"},
        headers={"X-Trigger-Token": TOKEN},
    )
    assert response.status_code == 202
    assert recorder.calls[0]["mode"] is ResearchRunMode.MANUAL


def test_a_wrong_or_missing_token_is_refused(http: TestClient, recorder: _Recorder) -> None:
    wrong = http.post("/api/automation/research/runs", headers={"X-Trigger-Token": "x" * 40})
    missing = http.post("/api/automation/research/runs")

    assert (wrong.status_code, missing.status_code) == (401, 401)
    assert recorder.calls == []


def test_without_a_configured_token_the_router_does_not_exist(
    http_without_token: TestClient, recorder: _Recorder
) -> None:
    response = http_without_token.post(
        "/api/automation/research/runs", headers={"X-Trigger-Token": ""}
    )
    assert response.status_code == 404
    assert recorder.calls == []


def test_a_second_trigger_while_one_is_active_is_a_conflict(
    http: TestClient, recorder: _Recorder
) -> None:
    recorder.active = True
    response = http.post("/api/automation/research/runs", headers={"X-Trigger-Token": TOKEN})
    assert response.status_code == 409
    assert "already queued" in response.json()["detail"]


def test_request_bounds_are_validated(http: TestClient, recorder: _Recorder) -> None:
    response = http.post("/api/console/research/runs", json={"targetArticleCount": 50})
    assert response.status_code == 422
    assert recorder.calls == []


def test_the_trend_parameters_reach_the_service(http: TestClient, recorder: _Recorder) -> None:
    response = http.post(
        "/api/automation/research/runs",
        json={
            "mode": "manual",
            "enabledSources": ["rss", "gdelt"],
            "maxRequestsPerSource": 10,
            "minTrendScore": 55,
            "minDataCoverage": 0.7,
            "queries": ["mouth taping", "  cold   plunge "],
            "geo": "GB",
        },
        headers={"X-Trigger-Token": TOKEN},
    )
    assert response.status_code == 202
    params = recorder.calls[0]["params"]
    assert params.enabled_sources == ["rss", "gdelt"]
    assert params.max_requests_per_source == 10
    assert (params.min_trend_score, params.min_data_coverage) == (55, 0.7)
    assert params.queries == ["mouth taping", "cold plunge"]


def test_an_unknown_source_or_too_many_queries_is_refused(
    http: TestClient, recorder: _Recorder
) -> None:
    unknown = http.post("/api/console/research/runs", json={"enabledSources": ["tiktok"]})
    many = http.post("/api/console/research/runs", json={"queries": ["q" * 3] * 11})
    assert (unknown.status_code, many.status_code) == (422, 422)
    assert recorder.calls == []


class _RunSession:
    def __init__(self, run: ResearchRun | None) -> None:
        self._run = run

    async def get(self, model: object, key: str) -> ResearchRun | None:
        return self._run


def _result_client(run: ResearchRun | None, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    app = create_app()

    async def _session() -> Any:
        yield _RunSession(run)

    async def _candidates(session: Any, run_id: str) -> list[Any]:
        return []

    monkeypatch.setattr(service, "candidates_for", _candidates)
    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[require_reviewer] = lambda: REVIEWER
    app.dependency_overrides[get_settings] = lambda: Settings(
        research_trigger_token=TOKEN, discovery_scan_enabled=False
    )
    return TestClient(app)


def test_the_result_endpoint_serves_the_trend_schema_to_n8n_and_the_desk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = ResearchRun(
        id="22222222-2222-2222-2222-222222222222", label="research_2026_10_12_abcd",
        mode=ResearchRunMode.WEEKLY, status=ResearchRunStatus.RUNNING, stage="enriching_topics",
        params={}, config={"trend_window_days": 7}, provider_status={}, stage_log=[], notes=[],
        error=None, created_at=datetime(2026, 10, 12, tzinfo=UTC),
        started_at=datetime(2026, 10, 12, tzinfo=UTC), finished_at=None, summary=None,
    )
    with _result_client(run, monkeypatch) as http:
        n8n = http.get(f"/api/automation/research/runs/{run.id}/result",
                       headers={"X-Trigger-Token": TOKEN})
        desk = http.get(f"/api/console/research/runs/{run.id}/result")
        refused = http.get(f"/api/automation/research/runs/{run.id}/result")
    assert n8n.status_code == desk.status_code == 200
    body = n8n.json()
    assert body["run_id"] == run.id and body["status"] == "running"
    assert "collection_window" in body and body["topics"] == []
    assert refused.status_code == 401


UNKNOWN_RUN = "00000000-0000-0000-0000-000000000000"


def test_the_result_of_an_unknown_run_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    with _result_client(None, monkeypatch) as http:
        response = http.get(f"/api/automation/research/runs/{UNKNOWN_RUN}/result",
                            headers={"X-Trigger-Token": TOKEN})
    assert response.status_code == 404
