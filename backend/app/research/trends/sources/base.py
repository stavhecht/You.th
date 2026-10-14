"""The one interface every trend source implements, and what wraps their calls.

A source turns a ``SourceQuery`` into normalised ``SourceItem``s
(``search``), and may also offer a long daily series for a baseline
(``volume``). What it can do is declared in ``capabilities``, so the graph
asks each source only what it can answer and a new source is a new class plus
a line in ``research/factory.py`` — nothing in the graph changes.

Three rules every implementation follows:

* **Never an empty list for a failure.** A source that could not answer raises
  ``ProviderUnavailable``; ``[]`` means "asked, and there was nothing". The
  graph records the two differently and never scores a failure as zero.
* **Permanent failures say so.** A rejected key or a spent quota raises with
  ``permanent=True``; the graph stops asking that source for the rest of the
  run.
* **No secrets in URLs.** httpx logs every request URL at INFO; keys go in
  headers (``logging_setup`` masks only a few parameter names).
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import httpx

from app.research.categories import Category
from app.research.providers.base import ProviderUnavailable
from app.research.trends.contracts import (
    Capability,
    DailySeries,
    SourceBatch,
    SourceQuery,
    SourceType,
)
from app.retrieval.base import ProviderError, RateLimited
from app.retrieval.throttle import EXHAUSTED_AFTER_SECONDS, HttpClient

logger = logging.getLogger(__name__)

#: Server errors worth one more try. 500 is usually a bug on the far side that
#: an identical request reproduces; these three are a gateway or an overload.
TRANSIENT_STATUSES = frozenset({502, 503, 504})


@dataclass(frozen=True, slots=True)
class DiscoveryPlan:
    """What a run sweeps for: seeds per category, manual queries, the window."""

    seeds: tuple[tuple[str, Category], ...]
    categories: tuple[Category, ...]
    manual: tuple[str, ...]
    since: datetime
    until: datetime
    geo: str
    language: str


class TrendSource(Protocol):
    name: str
    source_type: SourceType
    capabilities: frozenset[Capability]

    async def search(self, query: SourceQuery) -> SourceBatch:
        """Items for the query, newest first where the source can sort.

        Raises:
            ProviderUnavailable: the source could not answer.
        """
        ...


class DiscoverySource(TrendSource, Protocol):
    def discovery(self, plan: DiscoveryPlan) -> list[SourceQuery]:
        """The requests that sweep this source for a run, in order."""
        ...


class VolumeSource(TrendSource, Protocol):
    async def volume(self, query: SourceQuery) -> DailySeries:
        """Daily counts for the query between ``query.since`` and ``query.until``."""
        ...


class RequestBudget:
    """At most ``limit`` requests per source per run.

    Spent budget is reported as ``skipped: budget`` — unmeasured, never zero.
    """

    def __init__(self, limit: int, overrides: Mapping[str, int] | None = None) -> None:
        self._limit = limit
        self._overrides = dict(overrides or {})
        self._used: dict[str, int] = {}

    def take(self, source: str, count: int = 1) -> bool:
        limit = self._overrides.get(source, self._limit)
        used = self._used.get(source, 0)
        if used + count > limit:
            return False
        self._used[source] = used + count
        return True

    def used(self, source: str) -> int:
        return self._used.get(source, 0)


class TransientRetry:
    """Bounded retries for throttles, gateway errors, timeouts and dropped connections.

    Wraps a ``ThrottledClient`` built with ``max_retries=0``: that client keeps
    the pacing and the spent-quota fail-fast, and raises ``RateLimited`` at the
    first 429 instead of retrying on its own short schedule. Retrying here
    instead does two things the shared client cannot: wait a *per-source*
    minimum (GDELT refuses anything sooner than five seconds and sends no
    ``Retry-After``), and count every retry for the run's report.

    A spent quota (``Retry-After`` of an hour or more) is never retried.
    Exponential backoff with jitter, capped at ``max_delay``.
    """

    def __init__(
        self,
        http: HttpClient,
        *,
        attempts: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
        min_throttle_wait: float = 0.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._http = http
        self._attempts = max(1, attempts)
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._min_throttle_wait = min_throttle_wait
        self._sleep = sleep
        self.retries = 0

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        for attempt in range(self._attempts):
            last_attempt = attempt == self._attempts - 1
            wait_floor = 0.0
            try:
                response = await self._http.get(url, params=params, headers=headers)
            except RateLimited as exc:
                if last_attempt or _quota_spent(exc):
                    raise
                wait_floor = max(exc.retry_after or 0.0, self._min_throttle_wait)
            except (httpx.TimeoutException, httpx.TransportError):
                if last_attempt:
                    raise
            else:
                if response.status_code not in TRANSIENT_STATUSES or last_attempt:
                    return response
            self.retries += 1
            delay = min(self._max_delay, max(wait_floor, self._base_delay * 2**attempt))
            await self._sleep(delay + random.uniform(0, max(0.25, delay / 8)))
        raise AssertionError("unreachable")  # pragma: no cover


def _quota_spent(exc: RateLimited) -> bool:
    return (exc.retry_after or 0.0) >= EXHAUSTED_AFTER_SECONDS or "exhausted" in str(exc)


def unavailable(exc: BaseException, what: str) -> ProviderUnavailable:
    """The ``ProviderUnavailable`` for a transport or throttle failure.

    A spent quota (a ``Retry-After`` of an hour or more) is permanent for the
    run; anything else may succeed on the next query.
    """
    if isinstance(exc, RateLimited):
        return ProviderUnavailable(f"{what}: {exc}", permanent=_quota_spent(exc))
    if isinstance(exc, ProviderError | httpx.HTTPError):
        return ProviderUnavailable(f"{what}: {type(exc).__name__}: {exc}")
    return ProviderUnavailable(f"{what}: unexpected {type(exc).__name__}: {exc}")


def check_status(response: httpx.Response, what: str) -> None:
    """Raise for any response that is not a usable 2xx (or 304).

    401 and 403 are permanent — a key does not become valid mid-run.
    """
    status = response.status_code
    if status < 300 or status == 304:
        return
    if status in (401, 403):
        raise ProviderUnavailable(
            f"{what}: {status} (key rejected or access denied)", permanent=True
        )
    if status == 404:
        raise ProviderUnavailable(f"{what}: 404 not found", permanent=False)
    raise ProviderUnavailable(f"{what}: HTTP {status}")


async def fetch(http: HttpClient, url: str, what: str, **kwargs: Any) -> httpx.Response:
    """GET through the wrappers, converting every failure to ``ProviderUnavailable``."""
    try:
        response = await http.get(url, **kwargs)
    except ProviderUnavailable:
        raise
    except Exception as exc:
        raise unavailable(exc, what) from exc
    check_status(response, what)
    return response


def retries_of(http: object) -> int:
    """Transient retries a source's client made, for the run report."""
    return int(getattr(http, "retries", 0) or 0)


def log_skip(source: str, reason: str) -> None:
    logger.info("trend source %s skipped: %s", source, reason)
