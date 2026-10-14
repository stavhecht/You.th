"""Simple web search, through DuckDuckGo (the ``ddgs`` package). No key, no cost.

A discovery and coverage layer only. Results say what is being written about a
topic and by whom — they are never evidence for a health claim, and nothing
downstream treats them as such. ``ddgs`` scrapes, so it is slow (~8 s per
search, measured 2026-09-30) and can be throttled; the graph therefore only
web-searches the candidates still in contention (``research_web_pool_size``).

ddgs is synchronous; calls run in a thread, one at a time.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app.research.contracts import WebResult
from app.research.providers.base import ProviderUnavailable

logger = logging.getLogger(__name__)


def ddgs_region(geo: str, language: str) -> str:
    """``US``/``en`` -> ``us-en``, DuckDuckGo's region code."""
    country = "uk" if geo.upper() == "GB" else geo.lower()
    return f"{country}-{language.lower()}"


class SimpleWebSearchProvider:
    name = "duckduckgo"

    def __init__(
        self,
        *,
        timeout: int = 15,
        retries: int = 1,
        retry_wait: float = 5.0,
        client_factory: Callable[[], Any] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._timeout = timeout
        self._retries = retries
        self._retry_wait = retry_wait
        self._client_factory = client_factory or (lambda: _ddgs(timeout))
        self._sleep = sleep
        self._lock = asyncio.Lock()

    async def search(self, query: str, *, region: str, max_results: int) -> list[WebResult]:
        raw = await _call_ddgs(
            self._lock,
            lambda client: client.text(query, region=region, max_results=max_results),
            factory=self._client_factory,
            retries=self._retries,
            retry_wait=self._retry_wait,
            sleep=self._sleep,
            what=f"web search for {query!r}",
        )
        results: list[WebResult] = []
        for item in raw or []:
            url = str(item.get("href") or item.get("url") or "").strip()
            title = str(item.get("title") or "").strip()
            if url and title:
                results.append(
                    WebResult(title=title, url=url, snippet=str(item.get("body") or ""))
                )
        return results


def _ddgs(timeout: int) -> Any:
    from ddgs import DDGS

    return DDGS(timeout=timeout)


async def _call_ddgs(
    lock: asyncio.Lock,
    fn: Callable[[Any], Any],
    *,
    factory: Callable[[], Any],
    retries: int,
    retry_wait: float,
    sleep: Callable[[float], Awaitable[None]],
    what: str,
) -> Any:
    """One ddgs call in a thread, retried a bounded number of times.

    Shared by the web and news providers. Any exception counts as a failure to
    answer — ddgs raises its own types for throttling and timeouts, and a
    parsing error is no more of an answer than those are.
    """
    async with lock:
        last: BaseException | None = None
        for attempt in range(retries + 1):
            try:
                return await asyncio.to_thread(lambda: fn(factory()))
            except Exception as exc:
                if _no_results(exc):
                    # ddgs raises this exact message only when every engine
                    # answered and none had a result; an engine *error* is
                    # raised as itself instead. So this one is "asked, and
                    # there was nothing" — an empty answer, not a failure.
                    return []
                last = exc
                logger.info(
                    "%s failed (attempt %d/%d): %s", what, attempt + 1, retries + 1, exc
                )
                if attempt < retries:
                    await sleep(retry_wait)
        raise ProviderUnavailable(f"{what} failed: {type(last).__name__}: {last}") from last


def _no_results(exc: BaseException) -> bool:
    return type(exc).__name__ == "DDGSException" and str(exc) == "No results found."
