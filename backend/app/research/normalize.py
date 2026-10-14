"""Deterministic clean-up of raw trend queries: normalise, filter, cluster.

Everything here is plain code on purpose (no model call): the same input gives
the same candidates, and a wrong merge can be traced to a rule. The one model
call, ``triage.py``, runs on the clusters this produces — deciding what a query
*means* is the part that needs semantics; deciding that "creatine before bed"
and "Creatine before bed 2026" are one query does not.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field

from app.research.contracts import Category, RawTrend

_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_NON_WORD_RE = re.compile(r"[^a-z0-9\s-]")

#: Words that change a query's wording but not its topic.
_FILLER = frozenset({
    "a", "an", "the", "of", "for", "to", "in", "on", "with", "and", "or", "vs", "versus",
    "is", "are", "does", "do", "can", "should", "what", "how", "why", "when", "which",
    "who", "best", "top", "good", "bad", "really", "actually", "my", "your", "you", "i",
    "me", "we", "near", "reddit", "tiktok", "youtube", "guide", "tips", "list",
})

#: A query containing one of these is about shopping, markets, jobs or
#: litigation — not something a wellness article can answer. Whole words only.
#: "review" and "comparison" are product-shopping intent ("protein powder
#: review"): four of the first live run's five picks were such queries.
_NOT_A_TOPIC = frozenset({
    "review", "reviews", "comparison", "quote", "quotes", "hiring", "course", "courses",
    "price", "prices", "cost", "costs", "coupon", "discount", "deal", "deals", "sale",
    "amazon", "walmart", "costco", "target", "stock", "stocks", "shares", "lawsuit",
    "lawsuits", "login", "app", "apk", "ticket", "tickets",
    # Products and shops (measured 2026-10-12: "running shoes" ranked second,
    # "supplement store near me" reached triage). A product is bought, not
    # researched; its health question, if any, is phrased without the product.
    "shoe", "shoes", "sneaker", "sneakers", "headphones", "earbuds", "leggings",
    "apparel", "store", "stores", "shop", "shops", "brand", "brands",
})


#: Words that mark a query as being about health, fitness, food or the body.
#: A rising query must share a word with its seed or with this list to become a
#: candidate at all (``is_on_topic``). Measured 2026-09-30: Google Trends'
#: *rising* list for "fitness" and "protein" over seven days was mostly spam —
#: "learn golang", "online banking review", "cheap flights new york", each at
#: +7,000% — while the *top* list was sane. A filter that cannot be argued with
#: is the only defence that does not depend on a small model doing its job.
WELLNESS_TERMS = frozenset({
    "health", "healthy", "wellness", "wellbeing", "fitness", "fit", "exercise", "workout",
    "training", "train", "gym", "run", "running", "walk", "walking", "cardio", "strength",
    "muscle", "weight", "fat", "lean", "stretch", "stretching", "yoga", "pilates", "mobility",
    "recovery", "recover", "sauna", "cold", "plunge", "sleep", "insomnia", "nap", "melatonin",
    "diet", "nutrition", "nutrient", "food", "eat", "eating", "meal", "fasting", "fast",
    "keto", "protein", "fiber", "sugar", "carb", "calorie", "vitamin", "mineral",
    "supplement", "creatine", "magnesium", "zinc", "iron", "omega", "collagen", "probiotic",
    "prebiotic", "gut", "microbiome", "caffeine", "coffee", "tea", "matcha", "ashwagandha",
    "turmeric", "mushroom", "herbal", "stress", "anxiety", "cortisol", "mood", "mental",
    "brain", "cognitive", "memory", "focus", "meditation", "breathing", "breathwork",
    "blood", "pressure", "cholesterol", "glucose", "insulin", "diabetes", "heart", "cardiac",
    "inflammation", "immune", "immunity", "skin", "hair", "aging", "longevity", "hormone",
    "testosterone", "estrogen", "menopause", "pregnancy", "fertility", "joint", "bone",
    "pain", "injury", "posture", "hydration", "water", "electrolyte", "alcohol", "smoking",
    "vaping", "steps", "zone", "hiit", "peptide", "glp", "ozempic", "semaglutide",
})


def is_on_topic(query: str, seed: str) -> bool:
    """True when the query shares a word with its seed or with ``WELLNESS_TERMS``."""
    words = {_singular(word) for word in normalize_query(query).split()}
    anchors = {_singular(word) for word in normalize_query(seed).split()} | {
        _singular(term) for term in WELLNESS_TERMS
    }
    return bool(words & anchors)


def normalize_query(query: str) -> str:
    """Lowercase, accent-free, punctuation-free, years removed, spaces collapsed."""
    text = unicodedata.normalize("NFKD", query).encode("ascii", "ignore").decode()
    text = text.lower().replace("&", " and ")
    text = _YEAR_RE.sub(" ", text)
    text = _NON_WORD_RE.sub(" ", text)
    return " ".join(text.split())


def query_key(query: str) -> str:
    """Two queries with the same key are the same query.

    Filler words dropped, naive singular, word order ignored: "best creatine
    gummies", "creatine gummy" and "gummies creatine" all key to "creatine gummy".
    """
    words = [_singular(word) for word in normalize_query(query).split() if word not in _FILLER]
    return " ".join(sorted(set(words)))


def is_not_a_topic(query: str) -> bool:
    words = set(normalize_query(query).split())
    return bool(words & _NOT_A_TOPIC) or not query_key(query)


def singular(word: str) -> str:
    """Public face of the naive singular, for the trend search's phrase matching."""
    return _singular(word)


def _singular(word: str) -> str:
    """Naive English singular: enough to merge "gummies"/"gummy" and "oats"/"oat"."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


@dataclass
class QueryCluster:
    """Raw trends that name one topic. ``representative`` is the strongest."""

    cluster_id: int
    trends: list[RawTrend] = field(default_factory=list)

    @property
    def representative(self) -> RawTrend:
        return self.trends[0]

    @property
    def queries(self) -> list[str]:
        seen: dict[str, None] = {}
        for trend in self.trends:
            seen.setdefault(trend.query, None)
        return list(seen)

    @property
    def category(self) -> Category:
        return self.representative.category

    @property
    def strength(self) -> float:
        return trend_strength(self.representative)


def trend_strength(trend: RawTrend) -> float:
    """How strongly Trends reported a query rising, for ordering only.

    Breakouts first, then by rising percent; a seed's own related query
    outranks an expansion of one at equal growth.
    """
    base = 1e6 if trend.is_breakout else float(trend.rising_percent or 0)
    return base - trend.depth * 0.5


def _dropped(trend: RawTrend) -> bool:
    return (
        is_not_a_topic(trend.query)
        or not is_on_topic(trend.query, trend.seed)
        # The seed's own name ("sleep" under the seed "sleep") is the category,
        # not a trend in it: measured, it became topics called "Sleep",
        # "exercise" and "nutrition", each too broad to be one article.
        or query_key(trend.query) == query_key(trend.seed)
    )


def off_topic(trends: list[RawTrend]) -> list[RawTrend]:
    """The trends ``merge_exact`` will drop, for the run's metrics and notes."""
    return [trend for trend in trends if _dropped(trend)]


def merge_exact(trends: list[RawTrend]) -> list[QueryCluster]:
    """Drop non-topics and off-topic queries, merge the rest by key, strongest first."""
    ordered = sorted(trends, key=trend_strength, reverse=True)
    by_key: dict[str, QueryCluster] = {}
    for trend in ordered:
        if _dropped(trend):
            continue
        key = query_key(trend.query)
        cluster = by_key.get(key)
        if cluster is None:
            cluster = by_key[key] = QueryCluster(cluster_id=len(by_key))
        cluster.trends.append(trend)
    return list(by_key.values())


def merge_similar(
    clusters: list[QueryCluster], vectors: list[list[float]], threshold: float
) -> list[QueryCluster]:
    """Greedy single-pass merge of clusters whose representatives embed close.

    ``clusters`` must be strongest first (``merge_exact`` returns them so): each
    joins the first earlier cluster within ``threshold``, so a topic is named
    by its strongest query. Ids are renumbered 0..n-1 afterwards.
    """
    if len(clusters) != len(vectors):
        raise ValueError("one vector per cluster")
    kept: list[tuple[QueryCluster, list[float]]] = []
    for cluster, vector in zip(clusters, vectors, strict=True):
        for existing, existing_vector in kept:
            if cosine(vector, existing_vector) >= threshold:
                existing.trends.extend(cluster.trends)
                break
        else:
            kept.append((cluster, vector))
    merged = [cluster for cluster, _ in kept]
    for index, cluster in enumerate(merged):
        cluster.cluster_id = index
    return merged


def cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norms = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / norms if norms else 0.0


def fallback_subject(query: str) -> str:
    """The query minus filler words, for when no model named the subject."""
    words = [word for word in normalize_query(query).split() if word not in _FILLER]
    return " ".join(words) or normalize_query(query)
