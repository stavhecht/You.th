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
    "ginger": "Ginger",
    "curcumin": "Curcumin",
    "melatonin": "Melatonin",
    "magnesium": "Magnesium",
    "creatine": "Creatine",
    "beta-alanine": '"beta-Alanine"',
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
    # Not MeSH: the several wordings of one thing, since a quoted phrase matches only its
    # own. PubMed hits beside the outcome went 1 -> 109, 3 -> 105, 0 -> 147 and, for the
    # monitors, 53 -> 417 against the questions' wording (2026-11-07). Written on
    # evaluation cases, so they say nothing about names not listed here.
    **dict.fromkeys(
        ("ice bath", "ice baths", "cold plunge", "cold plunges"),
        '("cold water immersion"[tiab] OR "ice bath"[tiab] OR "ice baths"[tiab] OR '
        '"cold plunge"[tiab] OR "cold plunges"[tiab])',
    ),
    **dict.fromkeys(
        ("alcohol reduction", "reducing alcohol", "cutting down on alcohol"),
        '("alcohol reduction"[tiab] OR "reducing alcohol"[tiab] OR "reduced alcohol"[tiab])',
    ),
    "lemon water": '("lemon water"[tiab] OR "lemon juice"[tiab])',
    **dict.fromkeys(
        (
            "continuous glucose monitor",
            "continuous glucose monitors",
            "continuous glucose monitoring",
        ),
        '("continuous glucose monitoring"[tiab] OR "continuous glucose monitor"[tiab] OR '
        '"continuous glucose monitors"[tiab])',
    ),
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
        # How a question words its claim, not what papers are about (2026-11-05):
        # "replaces commercial gym" searched for papers with the word "replaces".
        "replace", "replaces", "detect", "detects", "measure", "measures",
        "track", "tracks", "accurate", "accurately",
        # "benefits women" is a claim about who, and its word is in no useful paper's title.
        "benefit", "benefits",
        # A claim's wording around timing and comparison ("when done before exercise",
        # "as much as running"): ANDed in, each word empties the search.
        "when", "done", "before", "after", "much",
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

#: Two names in one field. Quoted whole, each pair is a phrase no paper uses: "foam rolling
#: or fascia release". The comma needs a space after it, or "10,000 steps" is two names; the
#: slash needs letters both sides, or "16/8 intermittent fasting" is. Never a plain "and": it
#: joins a subject to its outcome ("vitamin d and depression", "protein timing and muscle
#: hypertrophy"), so each half would search on its own and find papers off the subject.
_NAME_SEPARATOR = re.compile(
    r"\s+(?:and/or|or|vs\.?|versus)\s+|(?<=[a-z])\s*/\s*(?=[a-z])|,\s+"
)

#: When, where and with what: "bulletproof coffee with butter or mct oil" names one drink.
_NAME_CONTEXT = re.compile(r"\s+(?:in|at|during|while|with|per|marketed)\s+.*$")

#: Always timing, even after one word: "stretching before or after a workout" is stretching.
_NAME_TIMING = re.compile(r"\s+(?:before|after)\s+.*$")

#: A bracket saying how much or how often: "sauna (3 times a week)", "fasting (low dose)".
_DOSE = re.compile(r"\d|\b(?:doses?|dosage|dosing|daily|weekly)\b")

#: Words of a name no abstract need contain for it to be about the thing.
_NAME_SMALL_WORDS = frozenset(
    {
        "a", "an", "and", "at", "day", "days", "in", "of", "on", "or", "the", "to", "while",
        "with", "your",
    }
)  # fmt: skip

_NAME_MIN_LONG_WORD = 4

#: A last word no paper's name for the thing needs: "vagus nerve stimulation devices".
_GENERIC_LAST_WORDS = frozenset(
    {
        "device", "devices", "treatment", "treatments", "product", "products", "system",
        "systems", "routine", "routines", "practice", "practices", "program", "programs",
        "programme", "programmes", "serum", "serums",
    }
)  # fmt: skip

#: Who a claim is about -> the words papers use. Hand-made, as ``MESH_HINTS`` is.
#: "without diabetes" had no review in PubMed; with "nondiabetic" beside it, 9 (2026-11-07).
POPULATION_HINTS: dict[str, str] = {
    "without diabetes": (
        '("without diabetes"[tiab] OR nondiabetic[tiab] OR "non-diabetic"[tiab])'
    ),
    "women": "(women[tiab] OR woman[tiab] OR female[tiab] OR females[tiab])",
    "men": "(men[tiab] OR male[tiab] OR males[tiab])",
    "older adults": '("older adults"[tiab] OR elderly[tiab] OR "older people"[tiab])',
    "children": "(children[tiab] OR pediatric[tiab] OR paediatric[tiab])",
    "teenagers": "(teenagers[tiab] OR adolescents[tiab])",
    "adolescents": "(teenagers[tiab] OR adolescents[tiab])",
    "pregnant": "(pregnant[tiab] OR pregnancy[tiab])",
    "pregnancy": "(pregnant[tiab] OR pregnancy[tiab])",
    "athletes": "(athletes[tiab] OR athletic[tiab])",
}

#: What a claim is compared with. It stays in the claim for ranking and the article, and
#: leaves the search: "skin aging effectively retinol" is in no abstract.
_COMPARISON = re.compile(
    r"\s+(?:as\s+\w+\s+as|(?:more|better|less|worse|safer)\s+than|than|compared\s+(?:with|to)|versus|vs\.?)\s+.*$"
)

#: What extraction is told to write when a question names no substance.
_NO_SUBJECT = frozenset({"unspecified", "none", "unknown", "n/a", "na"})

#: More words than any substance or practice name, unless it maps to a known one.
_MAX_NAME_WORDS = 8
_MAX_LISTED_NAME_WORDS = 4

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
        population, about = _population_group(claim)
        parts = (subject, population, _outcome_group(about))
        terms = " AND ".join(part for part in parts if part)
        return _review_and_general(claim, terms, self._min_year)

    def broaden(self, claim: str, product: str, ingredients: list[str]) -> list[SearchQuery]:
        """The substance alone, with no year limit: a claim short on recent
        work is exactly where an older trial is worth having."""
        subject = self._subject(claim, product, ingredients)
        population, _ = _population_group(claim)
        terms = f"{subject} AND {population}" if population else subject
        return _review_and_general(claim, terms, None)

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
        # "smart ring for sleep" -> the thing only: quoted whole, it matches nothing, and
        # a substance named after "for" is the outcome, not the subject.
        name = re.split(r"\s+for\s+", substance.strip().lower(), maxsplit=1)[0]
        if not name:
            continue
        # Asked of the whole field: split first, and each half of a long description
        # would pass as a name of eight words or fewer.
        is_a_name = _is_a_name(re.sub(r"\s*\([^)]*\)", "", name))
        for part in _names_within(name):
            # Context is no substance: "creatine during the menstrual cycle" is creatine.
            hint = MESH_HINTS.get(part) or _mesh_within(_NAME_CONTEXT.sub("", part))
            if hint:
                terms.append(hint)
            elif is_a_name:
                terms.extend(_name_term(form) for form in _word_forms(_name_core(part)))

    unique = list(dict.fromkeys(term for term in terms if term))
    return f"({' OR '.join(unique)})" if unique else ""


def _name_term(name: str) -> str:
    """A name in title and abstract: as a phrase, or with all its words apart.

    [tiab], or PubMed drops the quotes from a phrase it has not indexed and searches the
    loose words: "glazed-donut skin" found 1,059,975 papers on skin. The words apart, or a
    tagged phrase finds nothing for a real topic: "whole-body vibration plate training" had
    0 as a phrase and 46 so, while "glazed-donut skin" stayed at 0 (2026-11-07). The phrase
    stays for the providers without tags, where it asks for the words together.
    """
    words = [word for word in name.split() if word not in _NAME_SMALL_WORDS]
    # "one" AND "meal" AND "day" found 4,411 papers, the phrase 21: short words are everywhere.
    if sum(len(word) >= _NAME_MIN_LONG_WORD for word in words) < 2:
        return f'"{name}"[tiab]'
    together = " AND ".join(f'"{word}"[tiab]' for word in words)
    return f'("{name}"[tiab] OR ({together}))'


def _names_within(name: str) -> list[str]:
    """The separate names in a field: "ashwagandha (withania somnifera)" has two, and so
    has "foam rolling or fascia release"."""
    base = re.sub(r"\s*\([^)]*\)", "", name).strip()
    # An unmapped bracket beside a known substance or stating a dose qualifies, not names:
    # searched, "intermittent fasting (low dose)" found 357,271 papers (2026-11-07).
    brackets = [
        text
        for text in re.findall(r"\(([^)]+)\)", name)
        if _mesh_within(text) or not (_mesh_within(base) or _DOSE.search(text))
    ]
    return [part for text in (base, *brackets) for part in _split_name(text)]


def _split_name(text: str) -> list[str]:
    text = _NAME_TIMING.sub("", text)
    context = _NAME_CONTEXT.search(text)
    if context:
        # "sleeping with an onion in your sock" is defined by its context: bare "sleeping"
        # would find the whole sleep literature for a question nobody has studied.
        if len(text[: context.start()].split()) < 2:
            return [text]
        text = text[: context.start()]
    found = [part.strip() for part in _NAME_SEPARATOR.split(text) if part.strip()]
    # "hyaluronic acid serums or supplements" names one thing, not "supplements" as well.
    parts = [part for part in found if not set(part.split()) <= _USAGE_WORDS] or found
    # "hot or cold compresses": a lone word beside a longer name is half of that name, and
    # searched alone it finds everything saying "hot". A comma lists whole names.
    longer = [part for part in parts if " " in part]
    if ", " in text or not longer or len(longer) == len(parts):
        return parts
    last_word = longer[-1].split()[-1]
    return [
        part if " " in part or part in MESH_HINTS else f"{part} {last_word}" for part in parts
    ]


def _name_core(part: str) -> str:
    """A name without its usage words and generic last word. Never empties it."""
    words = [word for word in part.split() if word not in _USAGE_WORDS] or part.split()
    if len(words) > 1 and words[-1] in _GENERIC_LAST_WORDS:
        words.pop()
    return " ".join(words)


def _population_group(claim: str) -> tuple[str, str]:
    """Who the claim is about in indexed vocabulary, and the claim without them: as an
    outcome word, the "diabetes" of "without diabetes" finds the papers on diabetics.
    Not one of ``POPULATION_HINTS``: nothing is narrowed, as before."""
    lowered = claim.lower()
    found = [key for key in POPULATION_HINTS if re.search(rf"\b{key}\b", lowered)]
    if not found:
        return "", claim
    for key in found:
        lowered = re.sub(
            rf"\s*\b(?:(?:in|for|among)\s+)?(?:(?:people|adults|patients|those)\s+)?{key}\b",
            "",
            lowered,
        )
    # "pregnant women" is one group: as an OR, any study in women would qualify.
    pregnancy = {"pregnant", "pregnancy"} & set(found)
    named = [key for key in found if key != "women" or not pregnancy]
    hints = list(dict.fromkeys(POPULATION_HINTS[key] for key in named))
    return (hints[0] if len(hints) == 1 else f"({' OR '.join(hints)})"), lowered


def _word_forms(phrase: str) -> list[str]:
    """A name with its last word singular and plural. A quoted phrase matches one form
    only: measured 2026-11-05, "smart rings" found 17 papers where "smart ring" found 36.
    A form nobody uses ("citicolines") matches no paper only because it is tagged [tiab]:
    see ``_subject_group``. Words in
    -ing are activities ("foam rolling"), except the short ones ("smart ring"). A lone
    plural stays as written: "dates" -> "date" matches every abstract saying "to date"."""
    head, _, last = phrase.rpartition(" ")
    no_plural = last.endswith(("ss", "us", "is", "x", "ch", "sh")) or (
        last.endswith("ing") and len(last) > 4
    )
    lone_plural = not head and last.endswith("s")
    if len(last) < 3 or not last.isalpha() or no_plural or lone_plural:
        return [phrase]
    if last.endswith("ies") and len(last) > 4:
        other = f"{last[:-3]}y"
    elif last.endswith("s"):
        other = last[:-1]
    elif last.endswith("y") and last[-2] not in "aeiou":
        other = f"{last[:-1]}ies"
    else:
        other = f"{last}s"
    return [phrase, f"{head} {other}".strip()]


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
    # A list of short names is not a sentence, however many it lists; a half-sentence
    # ("doing yoga help muscle gain") runs longer than a name does.
    parts = _NAME_SEPARATOR.split(phrase)
    return len(words) <= _MAX_NAME_WORDS or (
        len(parts) > 1 and all(len(part.split()) <= _MAX_LISTED_NAME_WORDS for part in parts)
    )


def _mesh_within(phrase: str) -> str | None:
    """The known substances inside a longer name ("Creatine Monohydrate Powder"), so
    "creatine and beta-alanine" searches both.

    A key inside a longer match does not count, so "withania somnifera" beats
    "withania"; whole words only, so "zinc" does not match "zincate".
    """
    matches = [
        (key, term)
        for key, term in MESH_HINTS.items()
        if re.search(rf"\b{re.escape(key)}\b", phrase)
    ]
    kept = [
        term
        for key, term in matches
        if not any(key != other and key in other for other, _ in matches)
    ]
    return " OR ".join(dict.fromkeys(kept)) or None


def _content_words(text: str) -> list[str]:
    return [
        word
        for word in re.findall(r"[a-z0-9\-]+", text)
        if word not in _STOPWORDS and len(word) > 2
    ]


def _outcome_group(claim: str) -> str:
    """The claim's outcome in indexed vocabulary, or its keywords as a fallback."""
    lowered = _COMPARISON.sub("", claim.lower())
    # "is as effective as a multi-step routine" is nothing but its comparison.
    if not _content_words(lowered):
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
    words = _content_words(lowered)
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
