"""Normalisation and story grouping: URLs, timestamps, the four dedup layers."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.research.trends.contracts import SourceItem, SourceType
from app.research.trends.documents import (
    canonical_url,
    group_stories,
    make_item,
    parse_timestamp,
    registrable_domain,
    strip_publisher_suffix,
    title_key,
)

NOW = datetime(2026, 10, 12, 12, tzinfo=UTC)


def _item(
    title: str,
    url: str,
    *,
    publisher: str | None = None,
    when: datetime | None = NOW,
    kind: SourceType = SourceType.NEWS,
) -> SourceItem:
    item = make_item(
        source_name="t",
        source_type=kind,
        title=title,
        url=url,
        publisher=publisher,
        published_at=when,
        collected_at=NOW,
    )
    assert item is not None
    return item


# --- URLs -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("https://www.Example.com/a/?utm_source=x&utm_medium=y", "https://example.com/a"),
        ("http://example.com/a#comments", "https://example.com/a"),
        ("https://m.example.com/a?fbclid=1&gclid=2", "https://example.com/a"),
        ("https://amp.example.com/story/amp", "https://example.com/story"),
        ("https://example.com/a?id=7&utm_campaign=z", "https://example.com/a?id=7"),
        ("https://example.com/a?b=2&a=1", "https://example.com/a?a=1&b=2"),
        ("https://example.com:443/a", "https://example.com/a"),
        ("https://example.com:8443/a", "https://example.com:8443/a"),
        ("ftp://example.com/a", None),
        ("javascript:alert(1)", None),
        ("not a url", None),
    ],
)
def test_canonical_url(raw: str, canonical: str | None) -> None:
    assert canonical_url(raw) == canonical


def test_identifying_query_parameters_are_kept() -> None:
    """Many sites name an article by a parameter; only known trackers go."""
    assert canonical_url("https://site.example/view?p=123") == "https://site.example/view?p=123"
    assert canonical_url("https://site.example/view?p=1") != canonical_url(
        "https://site.example/view?p=2"
    )


@pytest.mark.parametrize(
    ("host", "domain"),
    [
        ("news.bbc.co.uk", "bbc.co.uk"),
        ("edition.cnn.com", "cnn.com"),
        ("www.abc.net.au", "abc.net.au"),
        ("example.com", "example.com"),
        (None, None),
    ],
)
def test_registrable_domain(host: str | None, domain: str | None) -> None:
    assert registrable_domain(host) == domain


# --- titles and timestamps --------------------------------------------------------


def test_the_publisher_suffix_is_stripped_only_when_it_names_the_publisher() -> None:
    assert strip_publisher_suffix("Creatine for women - Women's Health", "Women's Health") == (
        "Creatine for women"
    )
    assert strip_publisher_suffix("Push-ups - the guide", "Runner's World") == (
        "Push-ups - the guide"
    )
    curly = "Creatine: what\u2019s new? \u2013 Women's Health"  # curly quote, en dash
    assert title_key(curly, "Women's Health") == (
        "creatine whats new"  # the curly apostrophe folds away
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Sun, 11 Oct 2026 23:03:45 GMT", datetime(2026, 10, 11, 23, 3, 45, tzinfo=UTC)),
        ("2026-10-11T23:03:45Z", datetime(2026, 10, 11, 23, 3, 45, tzinfo=UTC)),
        ("2026-10-11T19:03:45-04:00", datetime(2026, 10, 11, 23, 3, 45, tzinfo=UTC)),
        ("20261011T230345Z", datetime(2026, 10, 11, 23, 3, 45, tzinfo=UTC)),
        (1760223825, datetime(2025, 10, 11, 23, 3, 45, tzinfo=UTC)),
        (
            time.struct_time((2026, 10, 11, 23, 3, 45, 6, 284, 0)),
            datetime(2026, 10, 11, 23, 3, 45, tzinfo=UTC),
        ),
        ("2026-10-11T23:03:45", datetime(2026, 10, 11, 23, 3, 45, tzinfo=UTC)),  # naive = UTC
        ("", None),
        ("yesterday", None),
        (-5, None),
    ],
)
def test_timestamps_become_timezone_aware_utc(value: object, expected: datetime | None) -> None:
    parsed = parse_timestamp(value, now=NOW)
    assert parsed == expected
    if parsed is not None:
        assert parsed.tzinfo is not None


def test_a_timestamp_from_the_future_is_dropped_not_trusted() -> None:
    tomorrow = NOW + timedelta(days=1)
    assert parse_timestamp(tomorrow.isoformat(), now=NOW) is None
    soon = NOW + timedelta(minutes=30)  # clock skew is tolerated
    assert parse_timestamp(soon, now=NOW) == soon
    other_zone = datetime(2026, 10, 12, 14, tzinfo=timezone(timedelta(hours=2)))
    assert parse_timestamp(other_zone, now=NOW) == NOW


def test_an_item_needs_a_title_and_an_http_link() -> None:
    assert (
        make_item(
            source_name="t",
            source_type=SourceType.NEWS,
            title="  ",
            url="https://a.example/x",
            collected_at=NOW,
        )
        is None
    )
    assert (
        make_item(
            source_name="t",
            source_type=SourceType.NEWS,
            title="Ok",
            url="mailto:a@b.c",
            collected_at=NOW,
        )
        is None
    )
    search = make_item(
        source_name="t",
        source_type=SourceType.SEARCH,
        title="creatine timing",
        collected_at=NOW,
    )
    assert search is not None and search.url is None


def test_descriptions_are_cleaned_and_truncated_never_kept_whole() -> None:
    item = make_item(
        source_name="t",
        source_type=SourceType.NEWS,
        title="T",
        url="https://a.example/x",
        collected_at=NOW,
        description="<p>" + "word " * 400 + "</p>",
    )
    assert item is not None and item.description is not None
    assert "<p>" not in item.description and len(item.description) <= 500


def test_naive_timestamps_cannot_enter_an_item() -> None:
    with pytest.raises(ValueError):
        SourceItem(
            source_name="t",
            source_type=SourceType.NEWS,
            title="t",
            collected_at=datetime(2026, 10, 12),
        )


# --- stories ----------------------------------------------------------------------


def test_the_four_layers_each_fold_their_own_duplicates() -> None:
    headline = "Creatine may help older adults sleep longer, a new trial finds"
    items = [
        _item(headline, "https://a.example/1"),
        _item(headline, "https://a.example/1"),  # exact
        _item(headline, "https://www.a.example/1?utm_source=feed"),  # canonical
        _item(headline, "https://b.example/wire"),  # title
        _item(
            "A new trial finds creatine may help older adults sleep longer",
            "https://c.example/z",
        ),  # near: same words, re-ordered
        _item("Magnesium and sleep: what we know", "https://d.example/m"),
    ]
    grouping = group_stories(items)
    assert grouping.removed == {
        "exact_url": 1,
        "canonical_url": 1,
        "title": 1,
        "near_duplicate": 1,
    }
    assert len(grouping.stories) == 2
    creatine = grouping.stories[grouping.story_of[0]]
    assert sorted(creatine.publisher_domains) == ["a.example", "b.example", "c.example"]


def test_syndicated_copies_are_one_story_however_many_sites_carry_it() -> None:
    wire = "Health officials warn about high-dose melatonin gummies for children"
    items = [_item(wire, f"https://site{i}.example/wire") for i in range(30)]
    grouping = group_stories(items)
    assert len(grouping.stories) == 1
    assert grouping.removed["title"] == 29


def test_templated_headlines_about_different_subjects_stay_apart() -> None:
    """Measured in development: "Readers ask whether X works" merged across topics."""
    items = [
        _item("Readers ask whether creatine actually works", "https://a.example/1"),
        _item("Readers ask whether ashwagandha actually works", "https://b.example/1"),
        _item("Readers ask whether magnesium actually works", "https://c.example/1"),
    ]
    assert len(group_stories(items).stories) == 3


def test_short_headlines_only_merge_exactly() -> None:
    items = [
        _item("Sleep tips", "https://a.example/1"),
        _item("Sleep tips 2", "https://b.example/1"),
    ]
    assert len(group_stories(items).stories) == 2


def test_search_queries_are_not_documents() -> None:
    query = make_item(
        source_name="google_trends",
        source_type=SourceType.SEARCH,
        title="creatine before bed",
        collected_at=NOW,
    )
    assert query is not None
    grouping = group_stories([query, _item("A story", "https://a.example/1")])
    assert 0 not in grouping.story_of
    assert len(grouping.stories) == 1


def test_the_earliest_report_represents_the_story() -> None:
    items = [
        _item("Same headline about zinc and colds", "https://late.example/1", when=NOW),
        _item(
            "Same headline about zinc and colds",
            "https://early.example/1",
            when=NOW - timedelta(days=2),
        ),
        _item("Same headline about zinc and colds", "https://undated.example/1", when=None),
    ]
    story = group_stories(items).stories[0]
    assert items[story.representative].publisher_domain == "early.example"
    assert story.published_at == NOW - timedelta(days=2)


def test_grouping_is_deterministic() -> None:
    items = [
        _item(f"Headline number {i % 7} about protein timing", f"https://s{i}.example/x")
        for i in range(40)
    ]
    first, second = group_stories(items), group_stories(list(items))
    assert first.story_of == second.story_of
    assert [s.item_ids for s in first.stories] == [s.item_ids for s in second.stories]
