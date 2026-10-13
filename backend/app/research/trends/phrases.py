"""Topic phrases from headlines. Deterministic: no model, no I/O.

The triage model was built and measured on short search queries ("creatine
before bed"), in batches of 25. Handed whole headlines it would see a dozen
shared generic words per line, which is the condition under which a 7B model
lumped 58 queries into "General Wellness and Health" (2026-09-30). So
headlines are first reduced to the regime triage was calibrated in: short
phrases, one to three words, that

* contain a **lexicon** word — a wellness term, a seed word, or the subject of
  a topic an earlier run already tracked; and
* appear in stories from at least ``research_phrase_min_publishers`` outlets
  (one by default), or match a query Google Trends reported rising;
* name something specific — a non-generic lexicon word, or a generic one with a
  short qualifier ("vitamin d"). A generic seed's own word ("sleep") is the
  category; a specific seed term ("creatine") is kept for triage to angle.

Discovery is broad on purpose. Measured 2026-10-12, a week of 356 feed stories
from 12+ outlets had no specific wellness term in two outlets' headlines:
outlets cover the same subjects in different words. So one outlet is enough to
*consider* a topic, and the cross-source requirement is applied at selection —
two or more independent outlets after enrichment has searched every source for
the topic — where it measures coverage rather than wording. A phrase is also
dropped when
a longer phrase covers exactly the same stories ("creatine supplementation"
beats "creatine supplementation" + "supplementation" seen in the same three
headlines), so one subject does not arrive as five near-identical groups.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from app.research.categories import Category
from app.research.normalize import _FILLER, WELLNESS_TERMS, normalize_query, query_key, singular
from app.research.trends.contracts import SourceItem
from app.research.trends.documents import StoryGrouping, strip_publisher_suffix

#: Longest phrase extracted, in words.
MAX_PHRASE_WORDS = 3
#: Words that never start or end a phrase: headline furniture, not subjects.
EDGE_STOPWORDS = frozenset(_FILLER) | frozenset(
    {
        "new",
        "study",
        "studies",
        "research",
        "researcher",
        "researchers",
        "scientist",
        "scientists",
        "says",
        "said",
        "say",
        "finds",
        "found",
        "find",
        "show",
        "shows",
        "could",
        "may",
        "might",
        "will",
        "would",
        "just",
        "more",
        "less",
        "most",
        "than",
        "after",
        "before",
        "about",
        "over",
        "under",
        "into",
        "out",
        "up",
        "down",
        "here",
        "heres",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "they",
        "their",
        "them",
        "our",
        "us",
        "his",
        "her",
        "he",
        "she",
        "people",
        "expert",
        "experts",
        "doctor",
        "doctors",
        "week",
        "weeks",
        "year",
        "years",
        "day",
        "days",
        "time",
        "way",
        "ways",
        "thing",
        "things",
        "help",
        "helps",
        "helped",
        "linked",
        "link",
        "according",
        "report",
        "reports",
        "news",
        "latest",
        "one",
        "two",
        "three",
        "first",
        "every",
        "all",
        "some",
        "many",
        "much",
        "very",
        "really",
        "also",
        "still",
        "even",
        "get",
        "gets",
        "got",
        "make",
        "makes",
        "take",
        "takes",
        "use",
        "uses",
        "using",
        "like",
        "know",
        "need",
        "want",
        "s",
        "t",
        "vs",
        "via",
        "per",
        "amid",
        "while",
        "without",
        "weigh",
        "weighs",
        "explain",
        "explains",
        "explained",
        "reveal",
        "reveals",
        "warn",
        "warns",
        "warning",
        "suggest",
        "suggests",
        "surprised",
        "worth",
        "claim",
        "claims",
        "reported",
        "trial",
        "trials",
    }
)
#: Wellness words too broad to be a topic on their own: "sleep" or "health"
#: alone is a category, not an article. Fine inside a longer phrase.
GENERIC_WORDS = frozenset(
    {
        "health",
        "healthy",
        "wellness",
        "wellbeing",
        "fitness",
        "fit",
        "food",
        "eat",
        "eating",
        "diet",
        "nutrition",
        "nutrient",
        "exercise",
        "workout",
        "training",
        "train",
        "mental",
        "body",
        "weight",
        "run",
        "running",
        "walk",
        "walking",
        "sleep",
        "stress",
        "pain",
        "blood",
        "heart",
        "brain",
        "skin",
        "hair",
        "water",
        "fat",
        "muscle",
        "strength",
        "cardio",
        "gym",
        "mood",
        "focus",
        "memory",
        "aging",
        "immune",
        "immunity",
        "recovery",
        "recover",
        "cold",
        "pressure",
        "zone",
        "steps",
        "fast",
        "lean",
        "mineral",
        "vitamin",
        "supplement",
        "injury",
        "joint",
        "bone",
        "hormone",
        "glucose",
        "sugar",
        "protein",
        "carb",
        "calorie",
        "meal",
        "cancer",
        "loss",
        "mobility",
        "flexibility",
        "endurance",
        "lifestyle",
    }
)


@dataclass
class Phrase:
    """One candidate topic phrase and the stories that use it."""

    text: str
    story_ids: set[int] = field(default_factory=set)
    #: Every item in those stories, copies included.
    item_ids: set[int] = field(default_factory=set)
    #: Distinct publishers of the stories' representatives.
    publishers: set[str] = field(default_factory=set)
    matches_trend: bool = False

    @property
    def support(self) -> int:
        return len(self.publishers)

    def category(self, items: Sequence[SourceItem]) -> Category:
        votes = Counter(c for i in self.item_ids if (c := items[i].category) is not None)
        return votes.most_common(1)[0][0] if votes else Category.OTHER

    def seed(self, items: Sequence[SourceItem]) -> str:
        votes = Counter(s for i in self.item_ids if (s := items[i].seed))
        return votes.most_common(1)[0][0] if votes else "headlines"


def lexicon(*extra: Iterable[str]) -> frozenset[str]:
    """Singular wellness terms plus the words of any extra terms given."""
    words = {singular(word) for word in WELLNESS_TERMS}
    for group in extra:
        for term in group:
            words.update(
                singular(word) for word in normalize_query(term).split() if len(word) > 2
            )
    return frozenset(words)


def extract_phrases(
    items: Sequence[SourceItem],
    grouping: StoryGrouping,
    vocabulary: frozenset[str],
    *,
    trend_keys: frozenset[str] = frozenset(),
    seed_keys: frozenset[str] = frozenset(),
    min_publishers: int = 1,
    limit: int = 150,
) -> list[Phrase]:
    """Phrases worth triaging, most widely reported first."""
    phrases: dict[str, Phrase] = {}
    for story in grouping.stories:
        lead = items[story.representative]
        publisher = lead.publisher_domain or f"story:{story.story_id}"
        title = strip_publisher_suffix(lead.title, lead.publisher)
        for text in _ngrams(normalize_query(title).split(), vocabulary):
            phrase = phrases.setdefault(text, Phrase(text=text))
            phrase.story_ids.add(story.story_id)
            phrase.item_ids.update(story.item_ids)
            phrase.publishers.add(publisher)

    kept: list[Phrase] = []
    for phrase in phrases.values():
        key = query_key(phrase.text)
        if not key:
            continue  # nothing left after filler
        if key in seed_keys and not _specific(phrase.text.split(), vocabulary):
            continue  # a generic seed's own word ("sleep", "diet") is the category
        # A *specific* seed term ("creatine", "sauna") that several outlets used
        # this week is kept: triage names its angle from the example headlines,
        # and one it cannot angle is discarded later as too broad. Measured
        # 2026-10-12: 356 stories from 12+ outlets shared almost no exact phrase
        # but their subjects, so dropping seed terms left feeds finding nothing.
        phrase.matches_trend = key in trend_keys
        if phrase.matches_trend:
            kept.append(phrase)  # people are searching for exactly this
        elif phrase.support >= min_publishers and _specific(phrase.text.split(), vocabulary):
            kept.append(phrase)

    kept = _closed(kept)
    kept.sort(key=lambda p: (-p.support, -len(p.text.split()), p.text))
    return kept[:limit]


def _ngrams(tokens: list[str], vocabulary: frozenset[str]) -> set[str]:
    found: set[str] = set()
    for size in range(1, MAX_PHRASE_WORDS + 1):
        for start in range(len(tokens) - size + 1):
            words = tokens[start : start + size]
            if words[0] in EDGE_STOPWORDS or words[-1] in EDGE_STOPWORDS:
                continue
            if words[0].isdigit() or words[-1].isdigit():
                continue
            if not any(singular(word) in vocabulary for word in words):
                continue
            if size == 1 and (singular(words[0]) in GENERIC_WORDS or words[0] in GENERIC_WORDS):
                continue
            if all(singular(word) in GENERIC_WORDS or word in EDGE_STOPWORDS for word in words):
                continue  # "healthy diet", "mental health": still a category
            found.add(" ".join(words))
    return found


def _specific(words: list[str], vocabulary: frozenset[str]) -> bool:
    """A phrase names a thing: a non-generic lexicon word, or a generic one qualified.

    "cold plunge" (plunge), "magnesium glycinate" (magnesium), "vitamin d" and
    "zone 2" (a generic word and a short qualifier) name something; "lower
    your blood", "popular weight" and "eating enough" — measured as ranked
    topics on 2026-10-12 — are fragments around a generic word.
    """
    if any(
        singular(word) in vocabulary
        and singular(word) not in GENERIC_WORDS
        and word not in GENERIC_WORDS
        for word in words
    ):
        return True
    return any(
        (singular(word) in GENERIC_WORDS or word in GENERIC_WORDS)
        and index + 1 < len(words)
        and len(words[index + 1]) <= 3
        and words[index + 1] not in EDGE_STOPWORDS
        for index, word in enumerate(words)
    )


def _closed(phrases: list[Phrase]) -> list[Phrase]:
    """Drop a phrase when a longer one containing it covers the same stories."""
    by_stories: dict[frozenset[int], list[Phrase]] = defaultdict(list)
    for phrase in phrases:
        by_stories[frozenset(phrase.story_ids)].append(phrase)
    kept: list[Phrase] = []
    for group in by_stories.values():
        for phrase in group:
            padded = f" {phrase.text} "
            if any(
                other is not phrase
                and len(other.text) > len(phrase.text)
                and padded in f" {other.text} "
                for other in group
            ):
                continue
            kept.append(phrase)
    return kept
