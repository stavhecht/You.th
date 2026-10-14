"""Web search through the Brave Search API — the documented alternative to ddgs.

``ddgs`` (``web_search.py``) scrapes DuckDuckGo's pages: keyless, but
undocumented and liable to break or be refused. Brave publishes a search API
with a free tier (an account and a subscription token; check its current plan
terms). When ``BRAVE_SEARCH_API_KEY`` is set the research agent uses this
instead, and ddgs stays the keyless fallback.

The token travels in the ``X-Subscription-Token`` header, never in the URL,
because httpx logs every request URL.
"""

from __future__ import annotations

import httpx

from app.research.contracts import WebResult
from app.research.providers.base import ProviderUnavailable
from app.retrieval.base import ProviderError, RateLimited
from app.retrieval.throttle import HttpClient

BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
#: Brave's per-request result ceiling.
BRAVE_MAX_COUNT = 20


class BraveSearchProvider:
    name = "brave"

    def __init__(self, http: HttpClient, api_key: str) -> None:
        self._http = http
        self._api_key = api_key

    async def search(self, query: str, *, region: str, max_results: int) -> list[WebResult]:
        country, _, language = region.partition("-")
        params = {
            "q": query,
            "count": str(min(BRAVE_MAX_COUNT, max(1, max_results))),
            "country": "GB" if country == "uk" else country.upper(),
            "search_lang": language or "en",
        }
        headers = {"Accept": "application/json", "X-Subscription-Token": self._api_key}
        try:
            response = await self._http.get(BRAVE_SEARCH_URL, params=params, headers=headers)
        except RateLimited as exc:
            raise ProviderUnavailable(f"brave search throttled: {exc}") from exc
        except (httpx.HTTPError, ProviderError) as exc:
            raise ProviderUnavailable(f"brave search failed: {exc}") from exc
        if response.status_code in (401, 403):
            raise ProviderUnavailable(
                f"brave search answered {response.status_code}: token rejected", permanent=True
            )
        if response.status_code >= 400:
            raise ProviderUnavailable(f"brave search answered {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderUnavailable("brave search returned non-JSON") from exc
        results = ((payload or {}).get("web") or {}).get("results") or []
        return [
            WebResult(
                title=str(item.get("title") or "").strip(),
                url=str(item.get("url") or ""),
                snippet=str(item.get("description") or ""),
            )
            for item in results
            if isinstance(item, dict) and item.get("title") and item.get("url")
        ]
