"""YouTube Data API v3 for social listening: find videos, then re-read their counters.

The official API only — no scraping. Quota: ``search.list`` is 100 units,
``videos.list`` is 1 unit per call of up to 50 ids, of a default 10,000 a day
shared with the trend search's YouTube source. So discovery is the scarce thing
(``social_youtube_max_searches_per_day``) and refreshing tracked videos is
nearly free.

YouTube does not report shares, and a channel can hide likes or switch off
comments: those come back ``None``, never 0. The key travels in a header,
never the URL (``logging_setup`` does not mask YouTube's ``key=``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from app.research.trends.sources.youtube import (
    SEARCH_URL,
    VIDEOS_URL,
    _error_reason,
    detect_youtube_quota,
)
from app.retrieval.base import ProviderError, RateLimited
from app.retrieval.throttle import HttpClient
from app.social.contracts import CollectionError, CollectionStatus

__all__ = ["VideoRecord", "YouTubeClient", "detect_youtube_quota"]

PLATFORM = "youtube"
PAGE_LIMIT = 50
_UNAVAILABLE_REASONS = frozenset(
    {"keyInvalid", "accessNotConfigured", "forbidden", "ipRefererBlocked", "keyExpired"}
)
_QUOTA_REASONS = frozenset({"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"})


@dataclass(frozen=True, slots=True)
class VideoRecord:
    video_id: str
    title: str
    description: str | None
    channel_id: str | None
    channel_title: str | None
    published_at: datetime | None
    views: int | None
    likes: int | None
    comments: int | None

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"


class YouTubeClient:
    def __init__(self, http: HttpClient, api_key: str) -> None:
        self._http = http
        self._headers = {"X-Goog-Api-Key": api_key, "Accept": "application/json"}

    async def search(
        self, query: str, *, since: datetime, until: datetime, language: str, region: str
    ) -> tuple[list[str], bool]:
        """Video ids published in [since, until], newest first, and whether the
        page was full (so the count is a floor)."""
        payload = await self._get(
            SEARCH_URL,
            {
                "part": "snippet",
                "type": "video",
                "q": query,
                "order": "date",
                "maxResults": str(PAGE_LIMIT),
                "publishedAfter": _stamp(since),
                "publishedBefore": _stamp(until),
                "relevanceLanguage": language.lower(),
                "regionCode": region.upper(),
            },
            f"youtube search {query!r}",
        )
        ids = [
            entry["id"]["videoId"]
            for entry in payload.get("items") or []
            if isinstance(entry, dict)
            and isinstance(entry.get("id"), dict)
            and entry["id"].get("videoId")
        ]
        return ids, len(ids) >= PAGE_LIMIT

    async def videos(self, ids: list[str]) -> list[VideoRecord]:
        """Snippet and counters for up to 50 ids. A deleted or private video is
        simply absent from the answer."""
        if not ids:
            return []
        payload = await self._get(
            VIDEOS_URL,
            {"part": "snippet,statistics", "id": ",".join(ids[:PAGE_LIMIT])},
            f"youtube videos ({len(ids)})",
        )
        return [r for r in (_record(v) for v in payload.get("items") or []) if r is not None]

    async def _get(self, url: str, params: dict[str, str], what: str) -> dict[str, Any]:
        try:
            response = await self._http.get(url, params=params, headers=self._headers)
        except RateLimited as exc:
            raise CollectionError(CollectionStatus.RATE_LIMITED, f"{what}: {exc}") from exc
        except (ProviderError, httpx.HTTPError) as exc:
            raise CollectionError(
                CollectionStatus.ERROR, f"{what}: {type(exc).__name__}: {exc}"
            ) from exc
        if response.status_code >= 400:
            reason = _error_reason(response) or ""
            if reason in _QUOTA_REASONS or response.status_code == 429:
                status = CollectionStatus.RATE_LIMITED
            elif reason in _UNAVAILABLE_REASONS or response.status_code in (401, 403):
                status = CollectionStatus.UNAVAILABLE
            else:
                status = CollectionStatus.ERROR
            raise CollectionError(
                status, f"{what}: HTTP {response.status_code} {reason}".strip()
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise CollectionError(CollectionStatus.ERROR, f"{what}: non-JSON response") from exc
        if not isinstance(payload, dict):
            raise CollectionError(CollectionStatus.ERROR, f"{what}: unexpected JSON")
        return payload


def _record(video: Any) -> VideoRecord | None:
    if not isinstance(video, dict) or not video.get("id"):
        return None
    snippet = video.get("snippet") or {}
    stats = video.get("statistics") or {}
    title = (snippet.get("title") or "").strip()
    if not title:
        return None
    return VideoRecord(
        video_id=str(video["id"]),
        title=title[:400],
        description=(snippet.get("description") or "")[:1000] or None,
        channel_id=snippet.get("channelId"),
        channel_title=snippet.get("channelTitle"),
        published_at=_parse_time(snippet.get("publishedAt")),
        views=_count(stats.get("viewCount")),
        likes=_count(stats.get("likeCount")),
        comments=_count(stats.get("commentCount")),
    )


def _count(value: Any) -> int | None:
    """YouTube sends counts as strings, and omits a hidden one entirely."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
