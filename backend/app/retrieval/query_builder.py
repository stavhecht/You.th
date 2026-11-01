"""Turn a claim into search queries. No model call, so a bad result can be
debugged by reading the query.

Every claim gets two queries: one restricted to reviews and meta-analyses, and
one general. Without the review-only pass, a few reviews lose the relevance
race against fifty primary studies and never reach ranking at all.

Every query must name the substance: ``<substance> AND <outcome>``. The
substance comes from the extracted ingredients, or from the product name when
there are none (small local models often return no ingredients). If neither
names anything, ``UnanchoredQuery`` is raised instead of searching the outcome
alone — an outcome-only query ("improves workout performance") returns real
papers about the wrong subject, and nothing downstream can tell.
"""

from __future__ import annotations

import re
from typing import Protocol

from app.retrieval.base import SearchQuery

#: Substance name -> the MeSH term papers are indexed under. Hand-made, not a
#: general MeSH lookup.
MESH_HINTS: dict[str, str] = {
    "ashwagandha": "Withania",
    "withania somnifera": "Withania",
    "withania": "Withania",
    "turmeric": "Curcuma",
    "curcumin": "Curcumin",
    "melatonin": "Melatonin",
    "magnesium": "Magnesium",
    "creatine": "Creatine",
    "rhodiola": "Rhodiola",
    "valerian": "Valerian",
    "l-theanine": "Theanine",
    "theanine": "Theanine",
    "omega-3": '"Fatty Acids, Omega-3"',
    "fish oil": '"Fish Oils"',
    "probiotics": "Probiotics",
    "collagen": "Collagen",
    "vitamin d": '"Vitamin D"',
    "zinc": "Zinc",
    "ginseng": "Panax",
    "sunscreen": '"Sunscreening Agents"',
    "menstrual cycle": '"Menstrual Cycle"',
}

#: Claim word -> the vocabulary papers actually use. "reduces stress" searches
#: poorly; "stress, psychological" and "cortisol" search well. Also read by
#: ``discovery/topics.py``, which only proposes outcomes listed here.
OUTCOME_HINTS: dict[str, str] = {
    "stress": '("stress, psychological"[MeSH] OR cortisol OR anxiety)',
    "anxiety": '("anxiety"[MeSH] OR anxiolytic)',
    "sleep": '("sleep"[MeSH] OR insomnia OR "sleep quality")',
    "energy": '(fatigue OR vitality OR "physical endurance")',
    "fatigue": '("fatigue"[MeSH] OR vitality)',
    "focus": '("cognition"[MeSH] OR attention OR "cognitive performance")',
    "memory": '("memory"[MeSH] OR "cognitive function")',
    "cognition": '("cognition"[MeSH] OR "cognitive performance")',
    "inflammation": '("inflammation"[MeSH] OR "c-reactive protein")',
    "immune": '("immune system"[MeSH] OR immunity)',
    "muscle": '("muscle strength"[MeSH] OR hypertrophy OR "lean mass")',
    "strength": '("muscle strength"[MeSH] OR "resistance training")',
    "weight": '("weight loss"[MeSH] OR "body composition" OR obesity)',
    "skin": '("skin aging"[MeSH] OR "skin elasticity" OR hydration)',
    "skin cancer": '("skin neoplasms"[MeSH] OR melanoma OR "skin cancer")',
    "hair": '("hair"[MeSH] OR alopecia)',
    "joint": '("arthralgia"[MeSH] OR "joint pain" OR osteoarthritis)',
    "gut": '("gastrointestinal microbiome"[MeSH] OR "digestive health")',
    "digestion": '("digestion"[MeSH] OR bloating)',
    "mood": '("affect"[MeSH] OR depression OR "mood")',
    "testosterone": "(testosterone OR androgen)",
    "blood sugar": '("blood glucose"[MeSH] OR "insulin resistance")',
    "cholesterol": '("cholesterol"[MeSH] OR "lipid profile")',
    "heart": '("cardiovascular diseases"[MeSH] OR "heart" OR "cardiovascular health")',
    "cardiovascular": '("cardiovascular diseases"[MeSH] OR "cardiovascular health")',
    "blood pressure": '("blood pressure"[MeSH] OR hypertension)',
    "bone": '("bone density"[MeSH] OR osteoporosis OR "bone health")',
    "recovery": '("muscle soreness" OR "exercise recovery" OR "delayed onset")',
    "endurance": '("physical endurance"[MeSH] OR "aerobic capacity" OR VO2)',
    "fitness": (
        '("physical fitness"[MeSH] OR "athletic performance"[MeSH] OR "exercise performance")'
    ),
}

_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
        "has", "have", "help", "helps", "improve", "improves", "in", "is",
        "it", "its", "of", "on", "or", "reduce", "reduces", "support",
        "supports", "that", "the", "this", "to", "with", "your",
        # Directional verbs (2026-10-23): a claim's direction is not its topic,
        # and "lowers blood pressure" must search the same papers as "raises".
        "lower", "lowers", "raise", "raises", "increase", "increases",
        "decrease", "decreases", "boost", "boosts", "prevent", "prevents",
        "cause", "causes", "worsen", "worsens", "enhance", "enhances",
        "promote", "promotes",
    }
)  # fmt: skip

#: Words a question opens with. A product "name" starting with one of these is
#: the user's question copied into the field, not a substance.
_INTERROGATIVES = frozenset(
    {
        "does", "do", "is", "are", "can", "could", "should", "would", "will", "what",
        "which", "how", "why", "when", "where", "who",
    }
)  # fmt: skip

#: How-it-is-taken words dropped from an unmapped product before it is quoted:
#: "daily sunscreen use" as one phrase missed the Nambour trial, titled "regular
#: sunscreen use" (2026-10-25). Never empties a name: all-usage keeps the original.
_USAGE_WORDS = frozenset(
    {
        "daily", "regular", "regularly", "use", "using", "taking", "intake",
        "supplement", "supplements", "supplementation", "topical", "at-home",
        # Never left alone: "dietary supplements" stays whole rather than "dietary".
        "dietary", "nutritional", "herbal",
    }
)  # fmt: skip

#: What extraction is told to write when a question names no substance.
_NO_SUBJECT = frozenset({"unspecified", "none", "unknown", "n/a", "na"})

#: More words than any substance or practice name, unless it maps to a known one.
_MAX_NAME_WORDS = 8

#: Reviews are scarce, so their pass needs fewer results; the general pass
#: carries the breadth. 30, not 15, since 2026-10-23: of 77 gold papers, mostly
#: meta-analyses, 24 never reached the pool (melatonin's 2013 meta-analysis on
#: sleep onset was not in the top 15 reviews for "melatonin AND sleep").
REVIEW_PASS_MAX_RESULTS = 30
GENERAL_PASS_MAX_RESULTS = 35


#: A claim about harm. Its own effect alone ("creatine AND kidney AND injury")
#: missed the reviews of a substance's whole safety record, the best sources for
#: "what are the side effects of X?": with these, creatine's ISSN safety position
#: stand and its misconceptions review reached the top 35 (2026-10-26).
HARM_CLAIM = re.compile(
    r"\b(?:causes?|damages?|harms?|injur\w*|toxic\w*|adverse|side effects?)\b"
)
_SAFETY_TERMS = (
    '("adverse effects"[sh] OR safety[tiab] OR "side effects"[tiab] OR "adverse events"[tiab])'
)


class UnanchoredQuery(RuntimeError):
    """Neither the ingredients nor the product name a substance to search for."""

    def __init__(self, claim: str, product: str, ingredients: list[str]) -> None:
        super().__init__(
            f"no searchable substance for claim {claim!r}: product {product!r} and "
            f"ingredients {ingredients!r} yielded no subject terms. Retrieval would "
            f"search the outcome only and return off-topic literature."
        )
        self.claim = claim
        self.product = product
        self.ingredients = ingredients


class QueryStrategy(Protocol):
    def build(self, claim: str, product: str, ingredients: list[str]) -> list[SearchQuery]:
        """The queries for one claim, reviews first. Raises ``UnanchoredQuery``."""
        ...

    def broaden(self, claim: str, product: str, ingredients: list[str]) -> list[SearchQuery]:
        """Wider queries for a claim that came back thin. Raises ``UnanchoredQuery``.

        Broadening drops the *outcome* and keeps the substance, never the other
        way round: relaxing the substance is exactly the off-topic query
        ``UnanchoredQuery`` exists to refuse.
        """
        ...


class TemplateQueryStrategy:
    """MeSH-aware boolean queries built from fixed templates."""

    def __init__(self, min_year: int | None = None) -> None:
        self._min_year = min_year

    def build(self, claim: str, product: str, ingredients: list[str]) -> list[SearchQuery]:
        """``<substance> AND <outcome>``, or just the substance if the claim has no keywords."""
        subject = self._subject(claim, product, ingredients)
        outcome = _outcome_group(claim)
        terms = f"{subject} AND {outcome}" if outcome else subject
        return _review_and_general(claim, terms, self._min_year)

    def broaden(self, claim: str, product: str, ingredients: list[str]) -> list[SearchQuery]:
        """The substance alone, with no year limit: a claim short on recent
        work is exactly where an older trial is worth having."""
        return _review_and_general(claim, self._subject(claim, product, ingredients), None)

    @staticmethod
    def _subject(claim: str, product: str, ingredients: list[str]) -> str:
        # Ingredients first: they name the actives, while a multi-ingredient
        # product's name is a brand with no literature behind it.
        subject = _subject_group(ingredients or [product])
        if not subject:
            raise UnanchoredQuery(claim, product, ingredients)
        return subject


class LLMQueryStrategy:
    """Model-written queries. Not wired up; kept only to mark the seam."""

    def build(self, claim: str, product: str, ingredients: list[str]) -> list[SearchQuery]:
        raise NotImplementedError("LLM query generation is deferred; see DESIGN.md §5")

    def broaden(self, claim: str, product: str, ingredients: list[str]) -> list[SearchQuery]:
        raise NotImplementedError("LLM query generation is deferred; see DESIGN.md §5")


def _review_and_general(claim: str, terms: str, min_year: int | None) -> list[SearchQuery]:
    """The two passes every claim gets: reviews only, then everything."""
    return [
        SearchQuery(
            claim=claim,
            terms=terms,
            reviews_only=True,
            max_results=REVIEW_PASS_MAX_RESULTS,
            min_year=min_year,
        ),
        SearchQuery(
            claim=claim,
            terms=terms,
            reviews_only=False,
            max_results=GENERAL_PASS_MAX_RESULTS,
            min_year=min_year,
        ),
    ]


def _subject_group(substances: list[str]) -> str:
    """OR the substances together, mapping known ones to MeSH terms."""
    terms: list[str] = []
    # "smart rings like the oura ring" -> both, each quoted: as one phrase it matched nothing.
    examples = (re.split(r"\s+(?:like|such as|including)\s+(?:the\s+)?", s) for s in substances)
    for substance in (part for parts in examples for part in parts):
        name = substance.strip().lower()
        if not name:
            continue
        # "ashwagandha (withania somnifera)" -> also try both halves.
        names = [name]
        if match := re.search(r"\(([^)]+)\)", name):
            names.append(match.group(1).strip())
            names.append(re.sub(r"\s*\([^)]*\)", "", name).strip())

        mapped = next((MESH_HINTS[n] for n in names if n in MESH_HINTS), None)
        mapped = mapped or _mesh_within(name)
        if mapped is None and not _is_a_name(name):
            continue
        core = " ".join(w for w in names[-1].split() if w not in _USAGE_WORDS)
        terms.append(mapped or f'"{core or names[-1]}"')

    unique = list(dict.fromkeys(term for term in terms if term))
    return f"({' OR '.join(unique)})" if unique else ""


def _is_a_name(phrase: str) -> bool:
    """False for something that cannot be a substance or practice name.

    Extraction sometimes copies the user's question into ``product``
    ("Is it bad for you?", or the eleven-word "Meta-analytic evidence on weekly
    resistance training volume and muscle hypertrophy"). Searched as a quoted
    phrase, that finds nothing, and the run publishes a confident "no
    evidence" verdict — measured 2026-10-01 on a question with a large
    literature. Refusing it makes the query unanchored instead: a visible
    failure rather than a false verdict. A known substance inside such a phrase
    is still found by ``_mesh_within`` first.
    """
    words = phrase.split()
    if not words or phrase.strip() in _NO_SUBJECT:
        return False
    if phrase.rstrip().endswith("?") or words[0] in _INTERROGATIVES:
        return False
    return len(words) <= _MAX_NAME_WORDS


def _mesh_within(phrase: str) -> str | None:
    """A known substance inside a longer name ("Creatine Monohydrate Powder").

    Longest match wins, so "withania somnifera" beats "withania"; whole words
    only, so "zinc" does not match "zincate".
    """
    matches = [
        (key, term)
        for key, term in MESH_HINTS.items()
        if re.search(rf"\b{re.escape(key)}\b", phrase)
    ]
    if not matches:
        return None
    return max(matches, key=lambda pair: len(pair[0]))[1]


def _outcome_group(claim: str) -> str:
    """The claim's outcome in indexed vocabulary, or its keywords as a fallback."""
    lowered = claim.lower()
    # Several can match ("improves sleep and reduces stress"): OR them all.
    # At a word start: "stress" inside "gastrointestinal distress" searched anxiety
    # papers (2026-10-26); plurals ("joints") still match.
    matched = [kw for kw in OUTCOME_HINTS if re.search(rf"\b{re.escape(kw)}", lowered)]
    # Longest match wins: "skin cancer" is not the skin-aging vocabulary of "skin".
    hints = list(
        dict.fromkeys(
            OUTCOME_HINTS[keyword]
            for keyword in matched
            if not any(keyword != other and keyword in other for other in matched)
        )
    )
    words = [
        word
        for word in re.findall(r"[a-z0-9\-]+", lowered)
        if word not in _STOPWORDS and len(word) > 2
    ]
    if len(hints) == 1 and len(words) > 1:
        # The claim's own phrase beside the hint: "reduces sleep latency" mapped
        # to the generic sleep vocabulary, and the meta-analysis on sleep onset
        # lost the relevance race. A one-word claim is the hint already, and a
        # claim naming two outcomes has no one phrase.
        hints.append(f'"{" ".join(words)}"[tiab]')
    if len(hints) > 1:
        group = f"({' OR '.join(hints)})"
    elif hints:
        group = hints[0]
    else:
        group = f"({' AND '.join(words)})" if words else ""
    if group and HARM_CLAIM.search(lowered):
        return f"({group} OR {_SAFETY_TERMS})"
    return group
