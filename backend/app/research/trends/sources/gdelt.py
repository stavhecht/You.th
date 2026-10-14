"""GDELT DOC 2.0 API: worldwide news article lists and daily coverage volume. No key.

``https://api.gdeltproject.org/api/v2/doc/doc``, two modes:

* ``artlist`` (discovery): up to 250 articles per request over a ``timespan``,
  each with url, title, ``seendate``, domain, language and source country.
  One OR-query per requested category.
* ``timelinevolraw`` (momentum): daily article counts for one topic over up to
  three months — the baseline the rest of the sources lack on a first run.

Raw counts are coverage volume, not independent reporting: GDELT counts every
syndicated copy. Independence is counted elsewhere, over deduplicated stories.

**GDELT answers errors as plain text**, not JSON, and with status 200 as often
as not: "Please limit requests to one every 5 seconds" (also as a 429 with no
``Retry-After``), "The specified phrase is too short", "Your search contained
a keyword that was too common". All of them are a failure to answer.

**``{}`` is treated as unmeasured, not as zero.** GDELT documents an empty
object as "no matches", but measured 2026-10-12 it answered ``{}`` for
``"creatine"`` over seven days while Google News listed 60 articles for the
same week, in the middle of a run of 429s. An empty answer from a server that
is shedding load and a genuine zero cannot be told apart, so the empty one is
reported as unavailable: a real zero lost costs one sub-signal, a false zero
would read as a collapse in coverage.

Pacing: GDELT asks for at most one request every five seconds per IP, so the
client runs at one per six (``GDELT_RPS`` in ``research/factory.py``) and the
retry wrapper waits at least ``THROTTLE_WAIT`` after a refusal. Measured
2026-10-12 from the development network: GDELT refused every request for
minutes at a time even at that spacing. The agent therefore treats GDELT as
optional — its failure costs a momentum sub-signal and some coverage, and the
run's data coverage says so.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from app.research.providers.base import ProviderUnavailable
from app.research.trends.contracts import (
    Capability,
    DailySeries,
    SourceBatch,
    SourceQuery,
    SourceType,
)
from app.research.trends.documents import make_item, parse_timestamp
from app.research.trends.sources.base import DiscoveryPlan, fetch
from app.retrieval.throttle import HttpClient

GDELT_DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
#: GDELT's per-request ceiling for article lists.
ARTLIST_MAX = 250
#: How far back the DOC API searches.
MAX_LOOKBACK = timedelta(days=90)
#: Seconds to wait after GDELT refuses a request before asking again.
THROTTLE_WAIT = 10.0

_LANGUAGES = {"en": "english", "es": "spanish", "fr": "french", "de": "german"}


def detect_gdelt_throttle(response: httpx.Response) -> float | None:
    """GDELT's "slow down" text on a 200, as a throttle for ``ThrottledClient``."""
    if response.status_code == 200 and b"limit requests" in response.content[:200]:
        return THROTTLE_WAIT
    return None


def gdelt_query(terms: tuple[str, ...], language: str) -> str:
    """``("vitamin d", "sleep")`` -> ``"vitamin d" sleep sourcelang:english``.

    A multi-word term is a phrase. Single words are ANDed, GDELT's default.
    """
    parts = []
    for term in terms:
        term = term.replace('"', "").strip()
        if term:
            parts.append(f'"{term}"' if " " in term else term)
    lang = _LANGUAGES.get(language.lower())
    return " ".join(parts) + (f" sourcelang:{lang}" if lang else "")


def gdelt_any_query(terms: list[str], language: str) -> str:
    """An OR over several terms, parenthesised as GDELT requires."""
    quoted = [
        f'"{t}"' if " " in t else t for t in (t.replace('"', "").strip() for t in terms) if t
    ]
    body = quoted[0] if len(quoted) == 1 else f"({' OR '.join(quoted)})"
    lang = _LANGUAGES.get(language.lower())
    return body + (f" sourcelang:{lang}" if lang else "")


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y%m%d%H%M%S")


class GdeltSource:
    name = "gdelt"
    source_type = SourceType.NEWS
    capabilities = frozenset({Capability.DISCOVER, Capability.SEARCH, Capability.VOLUME})

    def __init__(
        self, http: HttpClient, *, now: Callable[[], datetime] = lambda: datetime.now(UTC)
    ) -> None:
        self.http = http
        self._now = now

    def discovery(self, plan: DiscoveryPlan) -> list[SourceQuery]:
        """One OR-query per category over its seeds, then one per manual query."""
        by_category: dict[Any, list[str]] = defaultdict(list)
        for seed, category in plan.seeds:
            by_category[category].append(seed)
        queries = [
            SourceQuery(
                since=plan.since,
                until=plan.until,
                geo=plan.geo,
                language=plan.language,
                terms=tuple(seeds),
                target="any",
                seed=" | ".join(seeds),
                category=category,
                max_results=ARTLIST_MAX,
            )
            for category, seeds in by_category.items()
            if seeds
        ]
        queries += [
            SourceQuery(
                since=plan.since,
                until=plan.until,
                geo=plan.geo,
                language=plan.language,
                terms=(text,),
                seed=text,
                max_results=ARTLIST_MAX,
            )
            for text in plan.manual
        ]
        return queries

    async def search(self, query: SourceQuery) -> SourceBatch:
        text = (
            gdelt_any_query(list(query.terms), query.language)
            if query.target == "any"
            else gdelt_query(query.terms, query.language)
        )
        since = max(query.since, query.until - MAX_LOOKBACK)
        params = {
            "query": text,
            "mode": "artlist",
            "format": "json",
            "maxrecords": str(min(ARTLIST_MAX, query.max_results)),
            "startdatetime": _stamp(since),
            "enddatetime": _stamp(query.until),
            "sort": "datedesc",
        }
        payload = await self._json(params, f"gdelt articles for {query.text!r}")
        articles = payload.get("articles") or []
        if not isinstance(articles, list):
            raise ProviderUnavailable("gdelt: 'articles' is not a list")
        collected = self._now()
        items = []
        for article in articles:
            if not isinstance(article, dict):
                continue
            item = make_item(
                source_name=self.name,
                source_type=self.source_type,
                title=article.get("title"),
                url=article.get("url"),
                published_at=article.get("seendate"),
                collected_at=collected,
                raw_metadata={
                    key: article[key]
                    for key in ("language", "sourcecountry", "domain")
                    if article.get(key)
                },
                query=query.text,
                seed=query.seed,
                category=query.category,
            )
            if item is not None:
                items.append(item)
        return SourceBatch(
            items=items, total=len(articles), truncated=len(articles) >= ARTLIST_MAX
        )

    async def volume(self, query: SourceQuery) -> DailySeries:
        """Daily article counts, summed per UTC day whatever GDELT's resolution."""
        since = max(query.since, query.until - MAX_LOOKBACK)
        text = gdelt_query(query.terms, query.language)
        params = {
            "query": text,
            "mode": "timelinevolraw",
            "format": "json",
            "startdatetime": _stamp(since),
            "enddatetime": _stamp(query.until),
        }
        payload = await self._json(params, f"gdelt volume for {query.text!r}")
        days: dict[date, float] = defaultdict(float)
        timeline = payload.get("timeline") or []
        if not isinstance(timeline, list):
            raise ProviderUnavailable("gdelt: 'timeline' is not a list")
        series = next(
            (
                s
                for s in timeline
                if isinstance(s, dict) and "count" in str(s.get("series", "")).lower()
            ),
            timeline[0] if timeline else None,
        )
        for point in (series or {}).get("data") or []:
            if not isinstance(point, dict):
                continue
            when = parse_timestamp(point.get("date"), now=query.until + timedelta(days=1))
            value = point.get("value")
            if when is None or not isinstance(value, int | float):
                continue
            days[when.date()] += float(value)
        # Every day in the span: GDELT omits days with no articles from an
        # answered timeline, and those days are zeros inside a real answer.
        start, end = since.date(), query.until.date()
        span = [start + timedelta(days=n) for n in range((end - start).days + 1)]
        return DailySeries(
            source=self.name, query=text, dates=span, values=[days.get(d, 0.0) for d in span]
        )

    async def _json(self, params: dict[str, str], what: str) -> dict[str, Any]:
        response = await fetch(self.http, GDELT_DOC_URL, what, params=params)
        body = response.content.strip()
        if not body:
            raise ProviderUnavailable(f"{what}: empty response")
        try:
            payload = json.loads(body)
        except ValueError as exc:
            message = body[:120].decode("utf-8", "replace")
            raise ProviderUnavailable(f"{what}: GDELT said {message!r}") from exc
        if not isinstance(payload, dict):
            raise ProviderUnavailable(f"{what}: unexpected JSON")
        if not payload:
            raise ProviderUnavailable(
                f"{what}: GDELT answered {{}} (no data, or degraded; not counted as zero)"
            )
        return payload
