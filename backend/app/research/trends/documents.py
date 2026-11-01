"""Normalising source items and grouping copies into stories. Plain code, no I/O.

Four levels of "these are the same piece of reporting", strongest first:

1. **Exact URL.** The same link returned twice (a feed and a search).
2. **Canonical URL.** The same page behind tracking parameters, ``www.``,
   ``m.``, ``amp.`` or an AMP path.
3. **Title.** The same headline at different URLs — the common case for
   syndication (a wire story on thirty local sites), and the only identity
   Google News links have, since they are Google redirect URLs.
4. **Near-duplicate title.** A headline lightly re-edited ("Study finds X" /
   "X, study finds"): token Jaccard at or above ``NEAR_DUPLICATE_JACCARD``,
   set high enough that two headlines differing only in their subject stay
   two stories.

Items joined by any of these form one ``Story``. Independence is then counted
over stories, never items, so copies cannot read as corroboration.

Tracking parameters are stripped by name from a fixed list and nothing else
is touched: many sites identify an article by a query parameter (``?id=``,
``?p=``), and stripping blindly would merge different articles.
"""

from __future__ import annotations

import html
import math
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.research.categories import Category
from app.research.trends.contracts import SourceItem, SourceType, Story

#: Query parameters that only say how a reader arrived. Matched by exact name,
#: plus the ``utm_`` prefix.
TRACKING_PARAMS = frozenset(
    {
        "fbclid",
        "gclid",
        "dclid",
        "gbraid",
        "wbraid",
        "msclkid",
        "yclid",
        "twclid",
        "mc_cid",
        "mc_eid",
        "igshid",
        "ocid",
        "cmpid",
        "ref_src",
        "ref_url",
        "smid",
        "smtyp",
        "sr_share",
        "spm",
        "_ga",
        "_gl",
        "mkt_tok",
        "vero_id",
        "feature",
        "guccounter",
        "guce_referrer",
        "guce_referrer_sig",
        "outputtype",
        "taid",
        "soc_src",
        "soc_trk",
        "itm_source",
        "itm_medium",
        "itm_campaign",
        "at_medium",
        "at_campaign",
    }
)
_STRIPPED_HOST_PREFIXES = ("www.", "m.", "amp.", "mobile.")
#: Second-level public suffixes under which the registrable domain has three
#: labels. Not the full Public Suffix List — enough for the publishers a
#: wellness news sweep returns, and a miss only under-merges two outlets.
_SECOND_LEVEL_SUFFIXES = frozenset(
    {
        "co.uk",
        "org.uk",
        "ac.uk",
        "gov.uk",
        "nhs.uk",
        "me.uk",
        "ltd.uk",
        "plc.uk",
        "com.au",
        "net.au",
        "org.au",
        "edu.au",
        "gov.au",
        "co.nz",
        "org.nz",
        "govt.nz",
        "co.in",
        "net.in",
        "org.in",
        "gov.in",
        "co.za",
        "org.za",
        "gov.za",
        "com.br",
        "com.mx",
        "com.ar",
        "com.sg",
        "com.my",
        "com.ph",
        "com.hk",
        "com.tw",
        "co.jp",
        "or.jp",
        "ne.jp",
        "co.kr",
        "or.kr",
        "com.tr",
        "com.cn",
        "co.il",
        "org.il",
        "ac.il",
        "com.ng",
        "co.ke",
        "com.pk",
        "com.eg",
        "com.sa",
        "co.id",
        "or.id",
    }
)
#: Two headlines this similar (by distinct-token Jaccard) are one story. High
#: on purpose: templated headlines differ in exactly one word — the subject —
#: and "Readers ask whether creatine works" / "... whether ashwagandha works"
#: (0.71) are two stories about two topics. A light re-edit ("Study finds X
#: helps Y" / "X helps Y, study finds") shares every token and still merges.
NEAR_DUPLICATE_JACCARD = 0.8
#: Headlines shorter than this many distinct tokens are only merged exactly:
#: four-word headlines share half their words by accident.
NEAR_DUPLICATE_MIN_TOKENS = 5
#: A timestamp this far in the future is a broken feed, not news from tomorrow.
FUTURE_TOLERANCE = timedelta(hours=1)
DESCRIPTION_MAX = 500

_SPACE_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]+>")
_TITLE_TOKEN_RE = re.compile(r"[a-z0-9]+")
#: Words too common in headlines to say two of them are about one thing.
_TITLE_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "of",
        "for",
        "to",
        "in",
        "on",
        "at",
        "by",
        "with",
        "and",
        "or",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "it",
        "its",
        "this",
        "that",
        "as",
        "from",
        "how",
        "why",
        "what",
        "new",
        "study",
        "says",
        "finds",
        "could",
        "may",
        "can",
        "you",
        "your",
        "s",
    }
)


# --- single values ---------------------------------------------------------


def clean_text(value: object, *, limit: int | None = None) -> str:
    """Tags removed, entities unescaped, whitespace collapsed, optionally truncated.

    Unescaped because APIs escape their text: YouTube's titles arrive as
    "What&#39;s Worth Buying?" (measured 2026-10-13), and an escaped title both
    reads wrongly on the desk and misses the title layer of deduplication.
    """
    if not isinstance(value, str):
        return ""
    text = _SPACE_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", value))).strip()
    if limit is not None and len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def canonical_url(url: str) -> str | None:
    """The URL with transport noise removed, or None if it is not http(s).

    Lowercased scheme and host, ``www.``/``m.``/``amp.`` dropped, default port,
    fragment and trailing slash dropped, tracking parameters removed and the
    rest sorted, and an ``/amp`` path suffix folded into the page it mirrors.
    """
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower()
    for prefix in _STRIPPED_HOST_PREFIXES:
        if host.startswith(prefix) and host.count(".") >= 2:
            host = host[len(prefix) :]
            break
    port = parts.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    path = parts.path or "/"
    for suffix in ("/amp/", "/amp", ".amp"):
        if path.endswith(suffix) and len(path) > len(suffix):
            path = path[: -len(suffix)]
            break
    if len(path) > 1:
        path = path.rstrip("/")
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking(key)
    ]
    return urlunsplit(("https", host, path, urlencode(sorted(query)), ""))


def _is_tracking(key: str) -> bool:
    lowered = key.lower()
    return lowered.startswith("utm_") or lowered in TRACKING_PARAMS


def host_of(url: str | None) -> str | None:
    if not url:
        return None
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    return host.lower() if host else None


def registrable_domain(host: str | None) -> str | None:
    """``news.bbc.co.uk`` -> ``bbc.co.uk``; ``edition.cnn.com`` -> ``cnn.com``."""
    if not host:
        return None
    labels = host.lower().strip(".").split(".")
    if len(labels) < 2:
        return host.lower()
    if len(labels) >= 3 and ".".join(labels[-2:]) in _SECOND_LEVEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def strip_publisher_suffix(title: str, publisher: str | None = None) -> str:
    """Drop a trailing " - Publisher" (Google News appends one to every title)."""
    title = title.strip()
    if publisher:
        # hyphen, en dash, em dash, pipe
        for separator in (" - ", " \u2013 ", " \u2014 ", " | "):
            tail = f"{separator}{publisher.strip()}"
            if title.endswith(tail) and len(title) > len(tail):
                return title[: -len(tail)].strip()
    return title


def title_key(title: str, publisher: str | None = None) -> str:
    """Two items with the same key carry the same headline."""
    title = strip_publisher_suffix(title, publisher)
    text = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode().lower()
    return " ".join(_TITLE_TOKEN_RE.findall(text))


def title_tokens(key: str) -> frozenset[str]:
    return frozenset(word for word in key.split() if word not in _TITLE_STOPWORDS)


def parse_timestamp(value: Any, *, now: datetime | None = None) -> datetime | None:
    """A timezone-aware UTC datetime from what sources actually send.

    ISO 8601 (with or without ``Z``), RFC 822 (RSS ``pubDate``), GDELT's
    ``20261010T101500Z``, epoch seconds, a feedparser ``struct_time``, or a
    datetime. Naive values are taken as UTC. A value more than an hour in the
    future is a broken source and is dropped — never "news from tomorrow",
    which would read as the freshest item there is.
    """
    parsed = _parse(value)
    if parsed is None:
        return None
    parsed = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    reference = now or datetime.now(UTC)
    if parsed > reference + FUTURE_TOLERANCE:
        return None
    return parsed


def _parse(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, int | float) and not isinstance(value, bool):
        if not math.isfinite(value) or value <= 0:
            return None
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if hasattr(value, "tm_year"):  # time.struct_time (feedparser), always UTC
        try:
            return datetime(
                value.tm_year,
                value.tm_mon,
                value.tm_mday,
                value.tm_hour,
                value.tm_min,
                value.tm_sec,
                tzinfo=UTC,
            )
        except (TypeError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    compact = re.fullmatch(r"(\d{8})T(\d{6})Z?", text)
    if compact:
        try:
            return datetime.strptime("".join(compact.groups()), "%Y%m%d%H%M%S").replace(
                tzinfo=UTC
            )
        except ValueError:
            return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None


def make_item(
    *,
    source_name: str,
    source_type: SourceType,
    title: object,
    collected_at: datetime,
    url: object = None,
    publisher: object = None,
    publisher_url: object = None,
    published_at: Any = None,
    description: object = None,
    author: object = None,
    engagement: dict[str, float] | None = None,
    metrics: dict[str, float] | None = None,
    raw_metadata: dict[str, Any] | None = None,
    query: str | None = None,
    seed: str | None = None,
    category: Category | None = None,
    strip_publisher: bool = False,
) -> SourceItem | None:
    """A validated ``SourceItem``, or None when the record is unusable.

    Unusable means no title, or a URL that is present and not http(s). The
    caller counts those as malformed; it never invents a title or a link.
    ``publisher_url`` names the outlet when ``url`` does not (a Google News
    redirect), so the publisher domain comes from the outlet, not Google.
    """
    publisher_name = clean_text(publisher, limit=120) or None
    text = clean_text(title, limit=400)
    if strip_publisher:
        text = strip_publisher_suffix(text, publisher_name)
    if not text:
        return None
    link: str | None = None
    canonical: str | None = None
    if url is not None and url != "":
        if not isinstance(url, str):
            return None
        canonical = canonical_url(url)
        if canonical is None:
            return None
        link = url.strip()
    domain_source = publisher_url if isinstance(publisher_url, str) and publisher_url else link
    return SourceItem(
        source_name=source_name,
        source_type=source_type,
        title=text,
        url=link,
        canonical_url=canonical,
        publisher=publisher_name,
        publisher_domain=registrable_domain(host_of(domain_source)),
        published_at=parse_timestamp(published_at, now=collected_at),
        collected_at=collected_at,
        description=clean_text(description, limit=DESCRIPTION_MAX) or None,
        author=clean_text(author, limit=120) or None,
        engagement=engagement or {},
        metrics=metrics or {},
        raw_metadata=raw_metadata or {},
        query=query,
        seed=seed,
        category=category,
    )


# --- stories ---------------------------------------------------------------


class _UnionFind:
    def __init__(self, size: int) -> None:
        self._parent = list(range(size))

    def find(self, index: int) -> int:
        while self._parent[index] != index:
            self._parent[index] = self._parent[self._parent[index]]
            index = self._parent[index]
        return index

    def union(self, left: int, right: int) -> bool:
        a, b = self.find(left), self.find(right)
        if a == b:
            return False
        # The lower index wins, so the result never depends on visit order.
        if b < a:
            a, b = b, a
        self._parent[b] = a
        return True


class StoryGrouping:
    """Every document item assigned to a story, and what each layer merged."""

    def __init__(self, stories: list[Story], story_of: dict[int, int], removed: dict[str, int]):
        self.stories = stories
        #: Item index -> story id, for document items (search queries excluded).
        self.story_of = story_of
        #: Items folded into an earlier one, per layer.
        self.removed = removed


def group_stories(items: Sequence[SourceItem]) -> StoryGrouping:
    """Group document items into stories by the four layers, in order.

    Search queries (no URL) are not documents and are left out. Deterministic:
    the same items in the same order always give the same stories and ids.
    """
    documents = [i for i, item in enumerate(items) if item.url is not None]
    position = {item_index: n for n, item_index in enumerate(documents)}
    union = _UnionFind(len(documents))
    removed = {"exact_url": 0, "canonical_url": 0, "title": 0, "near_duplicate": 0}

    def link(layer: str, keyed: dict[str, list[int]]) -> None:
        for members in keyed.values():
            first = members[0]
            for other in members[1:]:
                if union.union(position[first], position[other]):
                    removed[layer] += 1

    link("exact_url", _bucket(documents, lambda i: items[i].url))
    link("canonical_url", _bucket(documents, lambda i: items[i].canonical_url))
    keys = {i: title_key(items[i].title, items[i].publisher) for i in documents}
    link("title", _bucket(documents, lambda i: keys[i] if len(keys[i]) >= 12 else None))
    _link_near_duplicates(documents, keys, position, union, removed)

    groups: dict[int, list[int]] = defaultdict(list)
    for item_index in documents:
        groups[union.find(position[item_index])].append(item_index)

    stories: list[Story] = []
    story_of: dict[int, int] = {}
    for story_id, root in enumerate(sorted(groups)):
        members = groups[root]
        representative = min(members, key=lambda i: _representative_key(items[i], i))
        dated = [when for i in members if (when := items[i].published_at) is not None]
        stories.append(
            Story(
                story_id=story_id,
                item_ids=members,
                representative=representative,
                title_key=keys[representative],
                publisher_domains=sorted(
                    {d for i in members if (d := items[i].publisher_domain)}
                ),
                source_types=sorted({items[i].source_type for i in members}),
                published_at=min(dated) if dated else None,
            )
        )
        for item_index in members:
            story_of[item_index] = story_id
    return StoryGrouping(stories, story_of, removed)


def _bucket(indexes: Iterable[int], key: Any) -> dict[str, list[int]]:
    buckets: dict[str, list[int]] = defaultdict(list)
    for index in indexes:
        value = key(index)
        if value:
            buckets[value].append(index)
    return buckets


def _link_near_duplicates(
    documents: list[int],
    keys: dict[int, str],
    position: dict[int, int],
    union: _UnionFind,
    removed: dict[str, int],
) -> None:
    """Jaccard over distinct headline tokens, compared only within a block.

    Blocking on each headline's two rarest tokens keeps this near-linear: two
    headlines that are near-duplicates share most tokens, so they share at
    least one of each other's rarest ones.
    """
    tokens = {i: title_tokens(keys[i]) for i in documents}
    frequency: dict[str, int] = defaultdict(int)
    for words in tokens.values():
        for word in words:
            frequency[word] += 1
    blocks: dict[str, list[int]] = defaultdict(list)
    for index in documents:
        words = tokens[index]
        if len(words) < NEAR_DUPLICATE_MIN_TOKENS:
            continue
        for word in sorted(words, key=lambda w: (frequency[w], w))[:2]:
            blocks[word].append(index)
    for block in blocks.values():
        if len(block) < 2 or len(block) > 400:
            continue
        for a_pos, a in enumerate(block):
            for b in block[a_pos + 1 :]:
                if union.find(position[a]) == union.find(position[b]):
                    continue
                if jaccard(tokens[a], tokens[b]) >= NEAR_DUPLICATE_JACCARD and union.union(
                    position[a], position[b]
                ):
                    removed["near_duplicate"] += 1


def jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _representative_key(item: SourceItem, index: int) -> tuple[int, datetime, int]:
    """Earliest-published first; undated after dated; then first collected."""
    if item.published_at is not None:
        return (0, item.published_at, index)
    return (1, item.collected_at, index)
