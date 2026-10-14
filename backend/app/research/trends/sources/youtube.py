"""YouTube Data API v3: what videos are being made about a topic, and watched.

Needs ``YOUTUBE_API_KEY`` (a Google Cloud API key with the YouTube Data API
enabled). The default quota is 10,000 units a day, and the two calls cost very
differently: ``search.list`` is **100 units**, ``videos.list`` is **1**. So a
search is the scarce thing — each topic gets one ``search.list`` (videos
published in the last two windows, newest first) and one batched
``videos.list`` for their statistics, and a run makes at most
``YOUTUBE_MAX_SEARCHES_PER_RUN`` searches (10 by default: ~1,010 units).

Momentum is uploads in the recent window against the window before. Audience
is the median views *per day since upload* of the recent videos. **Lifetime
views are never used as momentum**: a five-year-old video's total says
nothing about this week.

The key goes in the ``X-Goog-Api-Key`` header. ``logging_setup`` masks
``api_key=`` and ``token=`` in logged URLs but not YouTube's ``key=``, and
httpx logs every request URL.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from app.research.providers.base import ProviderUnavailable
from app.research.trends.contracts import (
    Capability,
    SourceBatch,
    SourceQuery,
    SourceType,
)
from app.research.trends.documents import make_item
from app.research.trends.sources.base import unavailable
from app.retrieval.throttle import HttpClient

SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
#: ``search.list`` page ceiling.
PAGE_LIMIT = 50
#: Error reasons that cannot be fixed by asking again today.
_PERMANENT_REASONS = frozenset(
    {
        "quotaExceeded",
        "dailyLimitExceeded",
        "keyInvalid",
        "accessNotConfigured",
        "forbidden",
        "ipRefererBlocked",
        "keyExpired",
    }
)


def detect_youtube_quota(response: httpx.Response) -> float | None:
    """A spent daily quota (403 ``quotaExceeded``) as an exhausted throttle.

    Returning a day's worth of seconds makes ``ThrottledClient`` fail every
    later call at once instead of spending the run asking.
    """
    if response.status_code == 403 and b"quotaExceeded" in response.content:
        return 86_400.0
    return None


class YouTubeSource:
    name = "youtube"
    source_type = SourceType.VIDEO
    capabilities = frozenset({Capability.SEARCH})

    def __init__(
        self,
        http: HttpClient,
        api_key: str,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.http = http
        self._key = api_key
        self._now = now

    async def search(self, query: SourceQuery) -> SourceBatch:
        what = f"youtube search for {query.text!r}"
        headers = {"X-Goog-Api-Key": self._key, "Accept": "application/json"}
        params = {
            "part": "snippet",
            "type": "video",
            "q": query.text,
            "order": "date",
            "maxResults": str(min(PAGE_LIMIT, query.max_results)),
            "publishedAfter": query.since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "publishedBefore": query.until.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "relevanceLanguage": query.language.lower(),
            "regionCode": query.geo.upper(),
        }
        found = await self._get(SEARCH_URL, params, headers, what)
        entries = [
            entry
            for entry in found.get("items") or []
            if isinstance(entry, dict) and (entry.get("id") or {}).get("videoId")
        ]
        stats: dict[str, dict[str, Any]] = {}
        if entries:
            ids = ",".join(entry["id"]["videoId"] for entry in entries)
            details = await self._get(
                VIDEOS_URL, {"part": "statistics", "id": ids}, headers, f"{what} (statistics)"
            )
            for video in details.get("items") or []:
                if isinstance(video, dict) and video.get("id"):
                    stats[video["id"]] = video.get("statistics") or {}

        collected = self._now()
        items = []
        for entry in entries:
            video_id = entry["id"]["videoId"]
            snippet = entry.get("snippet") or {}
            counts = stats.get(video_id) or {}
            item = make_item(
                source_name=self.name,
                source_type=self.source_type,
                title=snippet.get("title"),
                url=f"https://www.youtube.com/watch?v={video_id}",
                publisher=snippet.get("channelTitle"),
                published_at=snippet.get("publishedAt"),
                description=snippet.get("description"),
                collected_at=collected,
                engagement={
                    name: float(counts[key])
                    for name, key in (
                        ("views", "viewCount"),
                        ("likes", "likeCount"),
                        ("comments", "commentCount"),
                    )
                    if _is_number(counts.get(key))
                },
                query=query.text,
                seed=query.seed,
                category=query.category,
            )
            if item is not None:
                items.append(item)
        total = (found.get("pageInfo") or {}).get("totalResults")
        return SourceBatch(
            items=items,
            total=int(total) if isinstance(total, int) else len(items),
            truncated=len(entries) >= min(PAGE_LIMIT, query.max_results),
        )

    async def _get(
        self, url: str, params: dict[str, str], headers: dict[str, str], what: str
    ) -> dict[str, Any]:
        try:
            response = await self.http.get(url, params=params, headers=headers)
        except Exception as exc:
            raise unavailable(exc, what) from exc
        if response.status_code >= 400:
            reason = _error_reason(response)
            permanent = reason in _PERMANENT_REASONS or response.status_code in (401, 403)
            raise ProviderUnavailable(
                f"{what}: HTTP {response.status_code} {reason or ''}".strip(),
                permanent=permanent,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderUnavailable(f"{what}: non-JSON response") from exc
        if not isinstance(payload, dict):
            raise ProviderUnavailable(f"{what}: unexpected JSON")
        return payload


def _error_reason(response: httpx.Response) -> str | None:
    try:
        errors = (json.loads(response.content).get("error") or {}).get("errors") or []
    except (ValueError, AttributeError):
        return None
    return errors[0].get("reason") if errors and isinstance(errors[0], dict) else None


def _is_number(value: Any) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True
