"""Headline phrases: deterministic, cross-source, and short enough for triage."""

from __future__ import annotations

from datetime import UTC, datetime

from app.research.trends.contracts import SourceType
from app.research.trends.documents import group_stories, make_item
from app.research.trends.phrases import extract_phrases, lexicon

NOW = datetime(2026, 10, 12, tzinfo=UTC)


def _items(headlines: list[tuple[str, str]]) -> list:
    items = []
    for title, site in headlines:
        item = make_item(
            source_name="rss",
            source_type=SourceType.FEED,
            title=title,
            url=f"https://{site}/{abs(hash(title))}",
            published_at=NOW,
            collected_at=NOW,
        )
        assert item is not None
        items.append(item)
    return items


def _phrases(headlines: list[tuple[str, str]], **kwargs: object) -> list[str]:
    items = _items(headlines)
    found = extract_phrases(items, group_stories(items), lexicon(["creatine"]), **kwargs)
    return [p.text for p in found]


def test_a_phrase_two_outlets_used_becomes_a_candidate() -> None:
    found = _phrases(
        [
            ("Cold plunge benefits are overstated, physiologists say", "a.example"),
            ("Is the cold plunge worth the hype?", "b.example"),
            ("A cold plunge every morning: one writer's month", "c.example"),
        ]
    )
    assert "cold plunge" in found


def test_the_outlet_floor_is_applied_when_set() -> None:
    headlines = [
        ("Cold plunge benefits are overstated", "a.example"),
        ("Cold plunge every morning: a month", "a.example"),
    ]
    assert "cold plunge" not in _phrases(headlines, min_publishers=2)
    # One outlet is enough to be *considered* by default; selection later
    # needs two independent outlets after enrichment.
    assert "cold plunge" in _phrases(headlines)


def test_a_rising_search_lets_a_single_outlet_phrase_through() -> None:
    found = _phrases(
        [("Mouth taping for sleep, explained", "a.example")],
        trend_keys=frozenset({"mouth sleep taping"}),
    )
    assert "mouth taping for sleep" not in found  # 4 words: longer than a phrase
    found = _phrases(
        [("Why sleep taping went viral", "a.example")],
        trend_keys=frozenset({"sleep taping"}),
        min_publishers=2,
    )
    assert "sleep taping" in found  # people search for it, though one outlet used it


def test_generic_words_alone_are_categories_not_topics() -> None:
    found = _phrases(
        [
            ("Sleep is underrated, doctors say", "a.example"),
            ("Why sleep matters more than you think", "b.example"),
            ("Healthy diet tips for autumn", "c.example"),
            ("A healthy diet, simplified", "d.example"),
        ]
    )
    assert "sleep" not in found
    assert "healthy diet" not in found


def test_headline_furniture_never_starts_or_ends_a_phrase() -> None:
    found = _phrases(
        [
            ("New study finds magnesium helps sleep", "a.example"),
            ("New study finds magnesium helps sleep in older adults", "b.example"),
        ]
    )
    assert all(not p.startswith(("new ", "study ")) for p in found)
    assert all(not p.endswith((" finds", " study")) for p in found)


def test_a_generic_seed_is_the_category_and_a_specific_one_is_kept_for_triage() -> None:
    headlines = [
        ("Creatine is everywhere this autumn", "a.example"),
        ("What creatine does for women over 50", "b.example"),
        ("Sleep is the new status symbol", "c.example"),
        ("Why sleep trackers mislead", "d.example"),
    ]
    found = _phrases(headlines, seed_keys=frozenset({"creatine", "sleep"}))
    assert "creatine" in found  # triage angles it from the headlines, or it is too broad
    assert "sleep" not in found


def test_a_longer_phrase_covering_the_same_stories_wins() -> None:
    found = _phrases(
        [
            ("Zone 2 training is the new running fad", "a.example"),
            ("What zone 2 training does to your heart", "b.example"),
        ]
    )
    assert "zone 2 training" in found
    assert "zone 2" not in found  # same two stories, shorter


def test_most_widely_reported_first_and_deterministic() -> None:
    headlines = [
        ("Cold plunge craze grows", "a.example"),
        ("The cold plunge, examined", "b.example"),
        ("Cold plunge at home", "c.example"),
        ("Magnesium glycinate for sleep", "a.example"),
        ("Magnesium glycinate, explained", "b.example"),
    ]
    first, second = _phrases(headlines), _phrases(headlines)
    assert first == second
    assert first.index("cold plunge") < first.index("magnesium glycinate")


def test_fragments_around_a_generic_word_are_not_topics() -> None:
    """Measured: "lower your blood", "popular weight" and "eating enough" were ranked."""
    found = _phrases(
        [
            ("Ways to lower your blood sugar after dinner", "a.example"),
            ("Can you lower your blood pressure in a week?", "b.example"),
            ("A popular weight loss drug faces a shortage", "c.example"),
            ("The popular weight loss injection, explained", "d.example"),
            ("Are you eating enough protein?", "e.example"),
            ("Signs you are not eating enough", "f.example"),
        ]
    )
    assert "lower your blood" not in found
    assert "popular weight" not in found
    assert "eating enough" not in found


def test_a_generic_word_with_a_short_qualifier_names_a_thing() -> None:
    found = _phrases(
        [
            ("Vitamin D levels drop in autumn", "a.example"),
            ("How much vitamin D do you need?", "b.example"),
        ]
    )
    assert "vitamin d" in found
