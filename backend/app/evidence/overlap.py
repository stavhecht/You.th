"""How much of a sentence's wording its cited sources share.

Shared by VALIDATE's ``low_source_overlap`` warning and the evaluation's
groundedness score, so the two cannot measure it differently.
"""

from __future__ import annotations

import re

_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "been", "being", "but", "by", "can",
        "could", "did", "do", "does", "for", "from", "had", "has", "have", "in", "into",
        "is", "it", "its", "may", "might", "more", "most", "no", "not", "of", "on", "or",
        "our", "over", "such", "than", "that", "the", "their", "them", "then", "there",
        "these", "they", "this", "those", "to", "under", "up", "was", "we", "were", "what",
        "when", "which", "while", "who", "will", "with", "would", "also", "however", "both",
        "each", "other", "some", "only", "very", "one", "two", "three", "about", "after",
        "before", "between", "during", "against", "among",
    }
)  # fmt: skip


def _stem(word: str) -> str:
    for suffix in ("ations", "ation", "ingly", "ings", "ing", "edly", "ed", "es", "ly", "s"):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def content_terms(text: str) -> set[str]:
    words = re.findall(r"[a-z][a-z0-9\-]+", text.lower())
    return {_stem(word) for word in words if word not in _STOPWORDS and len(word) > 2}


def lexical_support(sentence: str, source_text: str) -> float:
    """Share of the sentence's content terms present in the source text."""
    terms = content_terms(sentence)
    if not terms:
        return 1.0
    return len(terms & content_terms(source_text)) / len(terms)
