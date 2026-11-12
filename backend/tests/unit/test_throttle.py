"""Pacing and 429 handling.

The thing under test is not really the token bucket — it is the guarantee that
a throttled provider can never look like a provider that returned no results.
That confusion is the one failure mode of this system with no visible symptom:
it produces a thinner evidence base, a more cautious verdict, and an article
that reads exactly like a correct one.

Retry timing is asserted against a recorded sleep rather than a real one, so
these run in milliseconds and assert the *chosen* delay instead of tolerating a
window around it.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from app.retrieval import throttle
from app.retrieval.base import RateLimited
from app.retrieval.pubmed import detect_throttle
from app.retrieval.throttle import (
    BASE_BACKOFF_SECONDS,
    MAX_JITTER_SECONDS,
    RateLimiter,
    ThrottledClient,
    retry_after_seconds,
)

_REQUEST = httpx.Request("GET", "https://example.org/search")


def _response(status: int = 200, *, text: str = "{}", **headers: str) -> httpx.Response:
    return httpx.Response(status, text=text, headers=headers, request=_REQUEST)


class _ScriptedClient:
    """Replays a list of responses, repeating the last one once exhausted."""

    def __init__(self, *responses: httpx.Response) -> None:
        self._responses = list(responses)
        self.calls = 0

    async def get(
        self, url: str, *, params: object = None, headers: object = None
    ) -> httpx.Response:
        self.calls += 1
        index = min(self.calls - 1, len(self._responses) - 1)
        return self._responses[index]


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record every backoff instead of serving it."""
    recorded: list[float] = []

    async def _sleep(delay: float) -> None:
        recorded.append(delay)

    monkeypatch.setattr(throttle.asyncio, "sleep", _sleep)
    return recorded


def _client(
    *responses: httpx.Response, **kwargs: object
) -> tuple[ThrottledClient, _ScriptedClient]:
    scripted = _ScriptedClient(*responses)
    # A rate high enough that the limiter never sleeps — these tests are about
    # the retry loop, and a real pause would only slow them down.
    return (
        ThrottledClient(
            scripted,
            RateLimiter(per_second=1_000_000),
            provider="testprovider",
            **kwargs,  # type: ignore[arg-type]
        ),
        scripted,
    )


# ---------------------------------------------------------------------------
# The bucket.
# ---------------------------------------------------------------------------


async def test_the_bucket_paces_sequential_requests() -> None:
    """Five acquisitions at 200/s cannot finish faster than four intervals.

    The first is free (the bucket starts full); each of the rest waits 5ms.
    """
    limiter = RateLimiter(per_second=200.0)

    started = time.monotonic()
    for _ in range(5):
        await limiter.acquire()
    elapsed = time.monotonic() - started

    assert elapsed >= 4 * (1 / 200.0)


async def test_concurrent_acquirers_are_paced_not_bunched() -> None:
    """The case the lock-across-the-sleep exists for.

    Ten tasks arriving together must still leave one interval between
    consecutive requests. Release the lock before sleeping and all ten read the
    same empty bucket, wait the same interval and fire at once — which is
    exactly the burst that gets an IP blocked.
    """
    limiter = RateLimiter(per_second=200.0)
    stamps: list[float] = []

    async def _one() -> None:
        await limiter.acquire()
        stamps.append(time.monotonic())

    await asyncio.gather(*(_one() for _ in range(10)))

    assert stamps[-1] - stamps[0] >= 9 * (1 / 200.0)


async def test_a_burst_allowance_lets_that_many_through_immediately() -> None:
    limiter = RateLimiter(per_second=10.0, burst=3)

    started = time.monotonic()
    for _ in range(3):
        await limiter.acquire()

    assert time.monotonic() - started < 0.05


def test_a_non_positive_rate_is_rejected_at_construction() -> None:
    """Zero would mean an infinite wait, which reads as a hung run."""
    with pytest.raises(ValueError, match="per_second"):
        RateLimiter(per_second=0)


async def test_the_limiter_is_consulted_once_per_attempt(sleeps: list[float]) -> None:
    """A retry is another request and must take another token.

    Retrying outside the limiter is how a client that is already being
    throttled sends its retries faster than its first attempts.
    """
    calls = 0
    limiter = RateLimiter(per_second=1_000_000)
    original = limiter.acquire

    async def _counted() -> None:
        nonlocal calls
        calls += 1
        await original()

    limiter.acquire = _counted  # type: ignore[method-assign]
    client = ThrottledClient(
        _ScriptedClient(_response(429), _response(429), _response(200)),
        limiter,
        provider="testprovider",
    )

    await client.get("https://example.org/search")

    assert calls == 3


# ---------------------------------------------------------------------------
# Retrying a throttle.
# ---------------------------------------------------------------------------


async def test_a_429_is_retried_and_the_call_succeeds(sleeps: list[float]) -> None:
    client, scripted = _client(_response(429), _response(200, text='{"ok": true}'))

    response = await client.get("https://example.org/search")

    assert response.status_code == 200
    assert scripted.calls == 2


async def test_retry_after_is_honoured_when_the_provider_sends_one(
    sleeps: list[float],
) -> None:
    """The header exists so we do not have to guess. ``retry_after`` was being
    carried on ``RateLimited`` and read by nothing before this."""
    client, _ = _client(_response(429, **{"retry-after": "4"}), _response(200))

    await client.get("https://example.org/search")

    assert sleeps == pytest.approx([4.0], abs=MAX_JITTER_SECONDS)
    assert sleeps[0] >= 4.0


async def test_backoff_grows_when_the_provider_gives_no_hint(
    sleeps: list[float],
) -> None:
    """Without a header, retrying at a flat interval just re-hits the limit."""
    client, _ = _client(_response(429), _response(429), _response(200))

    await client.get("https://example.org/search")

    assert len(sleeps) == 2
    assert sleeps[1] > sleeps[0]
    assert sleeps[0] >= BASE_BACKOFF_SECONDS


async def test_a_short_retry_after_never_shortens_our_own_backoff(
    sleeps: list[float],
) -> None:
    """Take the longer of the two, so a provider answering '0' on every 429
    cannot turn the retry loop into a spin."""
    client, _ = _client(_response(429, **{"retry-after": "0"}), _response(200))

    await client.get("https://example.org/search")

    assert sleeps[0] >= BASE_BACKOFF_SECONDS


async def test_the_wait_is_capped(sleeps: list[float]) -> None:
    """One throttled provider must not hold a pipeline run open."""
    client, _ = _client(
        _response(429, **{"retry-after": "600"}),
        _response(200),
        max_wait_seconds=3.0,
    )

    await client.get("https://example.org/search")

    assert sleeps == [3.0]


async def test_retries_are_bounded_and_the_last_throttle_raises(
    sleeps: list[float],
) -> None:
    """Raising is the point: the caller must not receive a body it would parse.

    A 429 body has no results in it, so returning it would enter the pipeline
    as "this claim has no literature".
    """
    client, scripted = _client(_response(429, **{"retry-after": "2"}), max_retries=2)

    with pytest.raises(RateLimited) as exc_info:
        await client.get("https://example.org/search")

    assert scripted.calls == 3  # the first attempt plus two retries
    assert exc_info.value.retry_after == 2.0
    assert len(sleeps) == 2


async def test_zero_retries_means_one_attempt(sleeps: list[float]) -> None:
    client, scripted = _client(_response(429), max_retries=0)

    with pytest.raises(RateLimited):
        await client.get("https://example.org/search")

    assert scripted.calls == 1
    assert sleeps == []


async def test_an_ordinary_response_passes_straight_through(
    sleeps: list[float],
) -> None:
    client, scripted = _client(_response(200, text='{"data": []}'))

    response = await client.get("https://example.org/search")

    assert response.text == '{"data": []}'
    assert (scripted.calls, sleeps) == (1, [])


async def test_a_server_error_is_not_retried_here(sleeps: list[float]) -> None:
    """500 is the provider's problem and each provider maps it to
    ``ProviderError`` itself. Retrying it here would hide that mapping."""
    client, scripted = _client(_response(503))

    response = await client.get("https://example.org/search")

    assert (response.status_code, scripted.calls) == (503, 1)


# ---------------------------------------------------------------------------
# Throttles that do not arrive as a 429.
# ---------------------------------------------------------------------------


async def test_a_provider_specific_throttle_signal_is_retried_like_a_429(
    sleeps: list[float],
) -> None:
    """PubMed's, specifically — 200 OK with an error string in the body.

    Without the detector this response is parsed: no ``idlist``, zero
    candidates, and the pipeline concludes the literature is empty.
    """
    throttled = _response(200, text='{"error": "API rate limit exceeded"}')
    client, scripted = _client(
        throttled, _response(200, text='{"esearchresult": {}}'),
        detect_throttle=detect_throttle,
    )

    response = await client.get("https://eutils.ncbi.nlm.nih.gov/x")

    assert "esearchresult" in response.text
    assert scripted.calls == 2


def test_pubmed_detects_its_own_throttle_body() -> None:
    assert detect_throttle(_response(200, text="API rate limit exceeded")) == 1.0
    assert detect_throttle(_response(200, text='{"esearchresult": {}}')) is None


# ---------------------------------------------------------------------------
# Header parsing.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ({"retry-after": "12"}, 12.0),
        ({"retry-after": "1.5"}, 1.5),
        ({"Retry-After": "3"}, 3.0),  # header names are case-insensitive
        ({}, 0.0),
        # The HTTP-date form is legal and none of these four APIs sends it.
        # 0.0 means "no hint", which falls back to exponential backoff.
        ({"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}, 0.0),
        ({"retry-after": "-5"}, 0.0),
    ],
)
def test_retry_after_parsing(header: dict[str, str], expected: float) -> None:
    assert retry_after_seconds(_response(429, **header)) == expected


async def test_an_exhausted_quota_fails_at_once_and_is_not_asked_again(
    sleeps: list[float],
) -> None:
    """OpenAlex said "come back in four hours" once its keyless budget ran out.
    Retrying in a few seconds cannot succeed; it only slows every claim."""
    client, scripted = _client(_response(429, **{"retry-after": "14849"}), _response(200))

    with pytest.raises(RateLimited):
        await client.get("https://example.org/search")
    with pytest.raises(RateLimited):
        await client.get("https://example.org/search")

    assert scripted.calls == 1  # the second call never left the process
    assert sleeps == []
