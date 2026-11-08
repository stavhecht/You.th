"""Collection statuses, the YouTube client against mocked HTTP, and the rotation."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.domain.models import SocialTopic
from app.research.contracts import EvidenceCounts
from app.research.providers.base import ProviderUnavailable
from app.research.providers.scientific import EuropePMCEvidenceProvider
from app.retrieval.base import RateLimited
from app.social.collect import platform_logs, rotation
from app.social.contracts import (
    CollectionError,
    CollectionStatus,
    EvidenceAssessment,
    SourceLog,
)
from app.social.questions import same_question, usable_question
from app.social.runner import execute_collection_run
from app.social.weekly import (
    LiteratureCheck,
    assess_evidence,
    found_no_papers,
    without_repeated_questions,
)
from app.social.youtube import YouTubeClient


class FakeHttp:
    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def get(self, url: str, *, params=None, headers=None) -> httpx.Response:
        self.calls.append({"url": url, "params": params, "headers": headers})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _json(status: int, body: Any) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(body).encode())


def _error(status: int, reason: str) -> httpx.Response:
    return _json(status, {"error": {"errors": [{"reason": reason}]}})


# --- statuses ----------------------------------------------------------------


def test_a_source_that_answered_with_nothing_is_empty_not_failed() -> None:
    log = SourceLog()
    log.ok(0)
    assert log.status is CollectionStatus.EMPTY


def test_one_rate_limited_call_marks_the_source_and_stops_it() -> None:
    log = SourceLog()
    log.ok(12)
    log.failed(CollectionError(CollectionStatus.ERROR, "boom"), "a")
    assert log.status is CollectionStatus.ERROR and not log.stopped
    log.failed(CollectionError(CollectionStatus.RATE_LIMITED, "quota"), "b")
    assert log.status is CollectionStatus.RATE_LIMITED and log.stopped


def test_a_source_never_asked_keeps_its_reason() -> None:
    logs = platform_logs(youtube_configured=False)
    assert logs["youtube"].status is CollectionStatus.NOT_CONFIGURED
    assert logs["instagram"].status is CollectionStatus.NOT_CONFIGURED
    assert logs["tiktok"].status is CollectionStatus.UNAVAILABLE
    assert "commercial" in logs["tiktok"].as_json()["detail"]
    assert platform_logs(youtube_configured=True)["youtube"].calls == 0


# --- YouTube -------------------------------------------------------------------


async def test_counters_are_parsed_and_a_hidden_one_is_none() -> None:
    http = FakeHttp(
        _json(
            200,
            {
                "items": [
                    {
                        "id": "abc",
                        "snippet": {
                            "title": "Creatine before bed?",
                            "channelId": "UC1",
                            "channelTitle": "Lab",
                            "publishedAt": "2026-11-01T10:00:00Z",
                        },
                        "statistics": {"viewCount": "1200", "commentCount": "4"},
                    },
                    {"id": "no-title", "snippet": {}, "statistics": {}},
                ]
            },
        )
    )
    [record] = await YouTubeClient(http, "secret").videos(["abc", "no-title"])
    assert record.views == 1200 and record.comments == 4
    assert record.likes is None  # hidden, not zero
    assert record.published_at == datetime(2026, 11, 1, 10, tzinfo=UTC)
    assert record.url == "https://www.youtube.com/watch?v=abc"


async def test_the_key_travels_in_a_header_never_the_url() -> None:
    http = FakeHttp(_json(200, {"items": []}))
    await YouTubeClient(http, "secret").search(
        "creatine",
        since=datetime(2026, 11, 1, tzinfo=UTC),
        until=datetime(2026, 11, 4, tzinfo=UTC),
        language="en",
        region="us",
    )
    call = http.calls[0]
    assert call["headers"]["X-Goog-Api-Key"] == "secret"
    assert "secret" not in json.dumps(call["params"])


async def test_a_full_page_is_reported_as_truncated() -> None:
    page = {"items": [{"id": {"videoId": f"v{i}"}} for i in range(50)]}
    ids, truncated = await YouTubeClient(FakeHttp(_json(200, page)), "k").search(
        "x",
        since=datetime(2026, 1, 1, tzinfo=UTC),
        until=datetime(2026, 1, 2, tzinfo=UTC),
        language="en",
        region="us",
    )
    assert len(ids) == 50 and truncated


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (_error(403, "quotaExceeded"), CollectionStatus.RATE_LIMITED),
        (_error(400, "keyInvalid"), CollectionStatus.UNAVAILABLE),
        (_error(403, "forbidden"), CollectionStatus.UNAVAILABLE),
        (_error(500, "backendError"), CollectionStatus.ERROR),
        (RateLimited("youtube", 86_400), CollectionStatus.RATE_LIMITED),
        (httpx.ConnectTimeout("slow"), CollectionStatus.ERROR),
    ],
)
async def test_each_failure_maps_to_its_status(
    response: Any, expected: CollectionStatus
) -> None:
    with pytest.raises(CollectionError) as caught:
        await YouTubeClient(FakeHttp(response), "k").videos(["a"])
    assert caught.value.status is expected


# --- rotation ------------------------------------------------------------------


def test_the_rotation_reaches_every_query_within_its_turn() -> None:
    queries = [f"q{i}" for i in range(37)]
    seen: set[str] = set()
    start = date(2026, 11, 1)
    for offset in range(4):  # ceil(37 / 10)
        chosen = rotation(queries, day=date.fromordinal(start.toordinal() + offset), per_day=10)
        assert len(chosen) == 10
        seen.update(chosen)
    assert seen == set(queries)


def test_a_short_watch_list_is_searched_whole_every_day() -> None:
    chosen = rotation(["b", "a", "A"], day=date(2026, 11, 1), per_day=15)
    assert chosen == ["A", "b"] or chosen == ["a", "b"]


def test_only_a_real_rate_limit_reads_as_rate_limited() -> None:
    from app.products.discover import provider_error
    from app.research.providers.base import ProviderUnavailable

    assert provider_error(ProviderUnavailable("could not generate a summary")).status is (
        CollectionStatus.ERROR
    )
    assert provider_error(ProviderUnavailable("RatelimitException: slow down")).status is (
        CollectionStatus.RATE_LIMITED
    )
    assert (
        provider_error(ProviderUnavailable("HTTP 429")).status is CollectionStatus.RATE_LIMITED
    )
    assert provider_error(ProviderUnavailable("401", permanent=True)).status is (
        CollectionStatus.UNAVAILABLE
    )


def test_a_source_that_was_asked_does_not_repeat_why_it_might_not_have_been() -> None:
    log = SourceLog(CollectionStatus.SUCCESS, "no new texts to read")
    assert log.as_json()["detail"] == "no new texts to read"
    log.ok(20)
    assert log.as_json()["detail"] is None


def test_a_topic_question_is_a_short_yes_no_question_about_its_subject() -> None:
    assert usable_question("Can smart rings detect sleep apnea?", "smart ring")
    assert usable_question("Are home gyms really worth it?", "home gym")
    # Not yes/no, not a question, about another subject.
    assert not usable_question("What happens to the brain without sleep?", "sleep")
    assert not usable_question("Smart rings detect sleep apnea", "smart ring")
    assert not usable_question("Does protein help build muscle?", "smart ring")
    assert not usable_question("Do " + "really " * 20 + "smart rings work?", "smart ring")


class _Counts:
    def __init__(self, name: str, *, down: bool = False) -> None:
        self.name, self.down, self.asked = name, down, []  # type: ignore[var-annotated]

    async def profile_terms(self, terms: str, recent_since: str) -> EvidenceCounts:
        self.asked.append(terms)
        if self.down:
            raise ProviderUnavailable(f"{self.name} count failed")
        return EvidenceCounts(
            source=self.name, query=terms, total=40, recent=6, reviews_or_meta=2, rcts=4
        )


class _Extraction:
    def __init__(self, product: str = "smart rings") -> None:
        self.asked: list[str] = []
        self.product = product

    async def extract(self, payload: Any) -> Any:
        self.asked.append(payload.topic)
        output = SimpleNamespace(
            product=self.product, ingredients=[], target_claims=["detects sleep apnea"]
        )
        return SimpleNamespace(output=output)


NOW = datetime(2026, 11, 5, tzinfo=UTC)
QUESTION = "Can smart rings detect sleep apnea?"


async def test_literature_is_counted_with_the_search_the_questions_article_would_run() -> None:
    extraction, pubmed = _Extraction(), _Counts("pubmed")
    check = LiteratureCheck(extraction, (pubmed, _Counts("europe_pmc")))  # type: ignore[arg-type]
    result = await assess_evidence(check, QUESTION, now=NOW)
    assert extraction.asked == [QUESTION]
    assert pubmed.asked == [result.query] and '"smart rings"' in pubmed.asked[0]
    assert result.status == "strong" and result.source == "pubmed"


async def test_europe_pmc_answers_when_pubmed_is_down_and_says_so() -> None:
    check = LiteratureCheck(
        _Extraction(),  # type: ignore[arg-type]
        (_Counts("pubmed", down=True), _Counts("europe_pmc")),  # type: ignore[arg-type]
    )
    result = await assess_evidence(check, QUESTION, now=NOW)
    assert result.source == "europe_pmc" and result.counts is not None


async def test_counts_nobody_could_make_are_not_checked_never_no_literature() -> None:
    check = LiteratureCheck(
        _Extraction(),  # type: ignore[arg-type]
        (_Counts("pubmed", down=True), _Counts("europe_pmc", down=True)),  # type: ignore[arg-type]
    )
    result = await assess_evidence(check, QUESTION, now=NOW)
    assert result.status == "not_checked" and result.counts is None
    assert "count failed" in str(result.reason)


async def test_a_question_with_no_searchable_subject_says_so_before_a_promotion() -> None:
    check = LiteratureCheck(_Extraction(product="unspecified"), (_Counts("pubmed"),))  # type: ignore[arg-type]
    result = await assess_evidence(check, "Is it good?", now=NOW)
    assert result.status == "not_checked" and "could not build a search" in str(result.reason)


async def test_europe_pmc_counts_the_pipelines_terms_in_titles_and_abstracts() -> None:
    class Provider:
        def __init__(self) -> None:
            self.asked: list[str] = []

        async def count(self, query: str) -> int:
            self.asked.append(query)
            return 0

    provider = Provider()
    counts = await EuropePMCEvidenceProvider(provider).profile_terms(  # type: ignore[arg-type]
        '("smart rings") AND (sleep[MeSH])', "2025-11-05"
    )
    assert counts.source == "europe_pmc" and counts.total == 0
    assert provider.asked == ['TITLE_ABS:(("smart rings") AND (sleep))']


def test_two_wordings_of_one_question_are_one_question() -> None:
    assert same_question(
        "Do smart rings accurately detect sleep apnea?", "Can smart rings detect sleep apnea?"
    )
    assert same_question(
        "Does a home gym replace a commercial gym?",
        "Does a smart home gym replace a commercial gym?",
    )
    assert same_question("Does creatine help sleep?", "Can creatine help sleep?")
    assert not same_question("Does creatine help sleep?", "Does magnesium help sleep?")
    assert not same_question(
        "Do smart rings accurately track sleep?", "Can smart rings detect sleep apnea?"
    )


def test_the_better_ranked_topic_keeps_a_repeated_question() -> None:
    first = SocialTopic(id="1", label="a", question="Can smart rings detect sleep apnea?")
    repeat = SocialTopic(id="2", label="b", question="Do smart rings detect sleep apnea?")
    other = SocialTopic(id="3", label="c", question="Does protein build muscle?")
    unwritten = SocialTopic(id="4", label="d", question=None)
    kept, repeats = without_repeated_questions([first, repeat, other, unwritten])
    assert kept == [first, other, unwritten] and repeats == [(repeat, first)]


def test_a_decided_topic_blocks_its_question_without_being_proposed() -> None:
    decided = SocialTopic(id="1", label="a", question="Can smart rings detect sleep apnea?")
    repeat = SocialTopic(id="2", label="b", question="Do smart rings detect sleep apnea?")
    other = SocialTopic(id="3", label="c", question="Does protein build muscle?")
    kept, repeats = without_repeated_questions([repeat, other], decided=[decided])
    assert kept == [other] and repeats == [(repeat, decided)]


@pytest.mark.parametrize("days", [20, 29, 30])
def test_retention_keeps_three_windows_and_a_margin_under_youtubes_limit(days: int) -> None:
    """The prune runs only with the daily collection: at 30, a post could reach almost 31."""
    with pytest.raises(ValueError, match="social_retention_days"):
        Settings(social_retention_days=days)


class _NoSession:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_: Any) -> bool:
        return False


async def _crash_during_collection(session: Any, report: Any, client: Any, **_: Any) -> None:
    report.stats["refreshed"] = 3
    raise RuntimeError("boom")


async def test_a_crash_ends_the_run_failed_with_what_it_had_so_far(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The desk's failed line and the scheduler's retry read what this records."""
    finished: dict[str, Any] = {}

    async def finish(_factory: Any, run_id: str, **fields: Any) -> None:
        finished.update(fields, run_id=run_id)

    monkeypatch.setattr("app.social.runner.collect_daily", _crash_during_collection)
    monkeypatch.setattr("app.social.service.finish", finish)

    await execute_collection_run(
        "run-1", "label", "social_daily", Settings(youtube_api_key=""), _NoSession  # type: ignore[arg-type]
    )

    assert finished["run_id"] == "run-1"
    assert finished["error"] == "RuntimeError: boom"
    assert finished["stats"] == {"refreshed": 3}


async def test_a_run_whose_end_cannot_be_recorded_does_not_take_the_worker_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker's loop does not guard this call. The stale sweep fails the row later."""

    async def finish(*_: Any, **__: Any) -> None:
        raise ConnectionRefusedError("database is restarting")

    monkeypatch.setattr("app.social.runner.collect_daily", _crash_during_collection)
    monkeypatch.setattr("app.social.service.finish", finish)

    await execute_collection_run(
        "run-1", "label", "social_daily", Settings(youtube_api_key=""), _NoSession  # type: ignore[arg-type]
    )


def test_only_a_search_that_ran_and_found_nothing_counts_as_no_papers() -> None:
    assert found_no_papers(EvidenceAssessment(status="none", counts={"total": 0}))
    assert not found_no_papers(EvidenceAssessment(status="limited", counts={"total": 2}))
    assert not found_no_papers(EvidenceAssessment(status="not_checked", reason="PubMed down"))
