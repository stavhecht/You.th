"""Deterministic reading of a generated article: statements, citations, numbers.

These checks need no model, so they cannot share the generator's blind spots,
and they are cheap enough to run on every sentence. They are deliberately
narrow — each answers one mechanical question:

* **Which sentence cites what.** Markers are parsed with the production
  ``CITATION_MARKER_RE``, so "cited" means exactly what the renderer and the
  validator mean by it.
* **Is every number in a cited sentence present in the sources it cites?** The
  sharpest hallucination signal available without a model: a statistic is
  either in the abstract (or the full-text excerpt the model was shown) or it
  was made up. Rounding is tolerated; a counted number of sources ("three
  trials") is not checked, since it is the model counting citations, not
  quoting a paper.
* **How much of a sentence's vocabulary appears in its sources.** A coarse
  lexical-support score. It cannot tell "reduced" from "did not reduce", which
  is why the LLM judge exists; it can tell a sentence that shares nothing with
  its source from one that paraphrases it.

The validator already checks that every *block* (beat 2, each section) carries
a citation. This module looks one level down, at sentences: a section with one
cited sentence and four uncited findings passes validation and fails
``citation_completeness`` here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.domain.contracts import CITATION_MARKER_RE, extract_handles
from app.evidence.numbers import source_numbers

#: Stands in for the full stop of an abbreviation while sentences are split.
_DOT = "\u2024"
_ABBREVIATIONS = re.compile(r"\b(e\.g|i\.e|et al|vs|approx|ca|cf|Fig|No|Dr|Mr|Ms)\.", re.I)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")
_LEADING_MARKERS = re.compile(rf"^((?:{CITATION_MARKER_RE.pattern}\s*)+)")

#: Words that make a sentence a report of a finding rather than framing.
_FINDING = re.compile(
    r"\b(found|finds|show(?:ed|s|n)?|report(?:ed|s)?|reduc(?:e|ed|es|tion)|"
    r"increas(?:e|ed|es)|improv(?:e|ed|es|ement)|decreas(?:e|ed|es)|lower(?:ed)?|"
    r"rais(?:e|ed)|no (?:significant )?(?:effect|difference|change|benefit)|"
    r"significant(?:ly)?|associat(?:ed|ion)|trials?|participants|subjects|patients|"
    r"meta-analys[ie]s|randomi[sz]ed|placebo|odds|risk|effect size|outcomes?)\b",
    re.I,
)

_NUMBER = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)")
_COUNT_NOUN = re.compile(
    r"^\s*(?:\w+\s){0,2}?(trials?|stud(?:y|ies)|reviews?|meta-analys[ie]s|sources|papers|"
    r"RCTs?|articles|analyses)\b",
    re.I,
)

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


@dataclass
class Statement:
    """One sentence of the article and the handles it cites."""

    location: str
    #: The sentence without citation markers, for number and vocabulary checks.
    text: str
    handles: list[str]
    #: True for beat 2 and sections — where the system's own rules say
    #: findings belong and must be cited.
    evidential: bool
    is_finding: bool = False
    numbers: list[str] = field(default_factory=list)
    #: The sentence as written, markers included — what the judge reads, since
    #: "a meta-analysis [S1] found" loses its subject without the marker.
    raw: str = ""


def split_sentences(text: str) -> list[str]:
    """Sentences, with a citation run that opens a sentence moved back to the
    one before it when it closes that sentence (``"… cortisol. [S1] Another …"``)
    — but kept where it is the next sentence's subject (``"[S6] also found …"``),
    which a lowercase word right after it gives away."""
    protected = _ABBREVIATIONS.sub(lambda m: m.group(0).replace(".", _DOT), text)
    pieces = [p.strip() for p in _SENTENCE_END.split(protected) if p.strip()]
    sentences: list[str] = []
    for piece in pieces:
        piece = piece.replace(_DOT, ".")
        match = _LEADING_MARKERS.match(piece)
        rest = piece[match.end() :].lstrip() if match else ""
        if match and sentences and not rest[:1].islower():
            sentences[-1] = f"{sentences[-1]} {match.group(1).strip()}"
            piece = piece[match.end() :].strip()
        if piece:
            sentences.append(piece)
    return sentences


def strip_markers(text: str) -> str:
    joined = " ".join(CITATION_MARKER_RE.sub("", text).split())
    return re.sub(r"\s+([.,;:!?)])", r"\1", joined)


def statements(draft: dict[str, Any]) -> list[Statement]:
    """Every sentence of the body, in reading order, with what it cites."""
    body = draft.get("body") or {}
    blocks: list[tuple[str, str, bool]] = [
        ("beat_1", body.get("beat_1_claim") or "", False),
        ("beat_2", body.get("beat_2_evidence") or "", True),
    ]
    for index, section in enumerate(body.get("sections") or [], start=1):
        blocks.append((f"section_{index}", section.get("body") or "", True))
    blocks.append(("beat_3", body.get("beat_3_bottom_line") or "", False))

    out: list[Statement] = []
    for location, text, evidential in blocks:
        for sentence in split_sentences(text):
            clean = strip_markers(sentence)
            if not clean:
                continue
            out.append(
                Statement(
                    location=location,
                    text=clean,
                    handles=sorted(extract_handles(sentence), key=lambda h: int(h[1:])),
                    evidential=evidential,
                    is_finding=is_finding(clean),
                    numbers=checkable_numbers(clean),
                    raw=sentence,
                )
            )
    return out


def is_finding(sentence: str) -> bool:
    return bool(_NUMBER.search(sentence) or _FINDING.search(sentence))


#: ".005" as abstracts write p-values, read as "0.005" on both sides.
_BARE_DECIMAL = re.compile(r"(?<![\w.])\.(\d)")


def _with_leading_zeros(text: str) -> str:
    return _BARE_DECIMAL.sub(r"0.\1", text)


def _normalise_number(raw: str) -> float:
    return float(raw.replace(",", ""))


def checkable_numbers(sentence: str) -> list[str]:
    """Numbers a source should contain: not counts of the sources themselves."""
    numbers: list[str] = []
    sentence = _with_leading_zeros(sentence)
    for match in _NUMBER.finditer(sentence):
        value = _normalise_number(match.group(1))
        tail = sentence[match.end() :]
        if value <= 50 and value.is_integer() and _COUNT_NOUN.match(tail):
            continue
        numbers.append(match.group(1))
    return numbers


def numbers_in(text: str) -> list[float]:
    """Every number in a source's text, digits and spelled-out words alike.

    Spelled-out numbers are read by the pipeline's own ``source_numbers``, so
    the eval and VALIDATE agree: a source saying "Twenty-nine trial comparisons"
    supports "29 comparisons" in both. Without it the eval flagged that
    sentence as misattributed while VALIDATE passed it (2026-10-22).
    """
    digits = [
        _normalise_number(m.group(1)) for m in _NUMBER.finditer(_with_leading_zeros(text))
    ]
    return digits + [n for n in source_numbers(text) if n not in digits]


def number_supported(raw: str, source_numbers: list[float]) -> bool:
    """Exact, or a rounding of a source value (27.9 written as 28 or 27.9)."""
    value = _normalise_number(raw)
    for candidate in source_numbers:
        if candidate == value:
            return True
        if value.is_integer() and abs(candidate - value) < 0.5:
            return True
        if round(candidate, 1) == value:
            return True
    return False


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


def contains_any(text: str, phrases: list[str]) -> list[str]:
    lowered = text.lower()
    return [phrase for phrase in phrases if phrase.lower() in lowered]


def draft_text(draft: dict[str, Any], *, markers: bool = False) -> str:
    """The whole article as plain text: headline, summary, body."""
    body = draft.get("body") or {}
    parts = [
        draft.get("headline") or "",
        draft.get("verdict_qualifier") or "",
        draft.get("summary") or "",
        body.get("beat_1_claim") or "",
        body.get("beat_2_evidence") or "",
    ]
    for section in body.get("sections") or []:
        parts.extend([section.get("heading") or "", section.get("body") or ""])
    parts.append(body.get("beat_3_bottom_line") or "")
    text = "\n".join(part for part in parts if part)
    return text if markers else strip_markers(text)


def labelled_article(draft: dict[str, Any]) -> str:
    """The article with each part named, for the judge.

    Every article opens by restating the claim it checks ("Vitamin C prevents
    the common cold.") and only then says what the research shows. Unlabelled,
    a reader — and the judge was measured doing it — takes that opening for the
    article's own assertion and scores a correct "no" as asserting the claim.
    """
    body = draft.get("body") or {}
    parts = [
        f"HEADLINE: {draft.get('headline') or ''}",
        f"VERDICT: {draft.get('verdict')}"
        + (f" ({draft['verdict_qualifier']})" if draft.get("verdict_qualifier") else ""),
        f"SUMMARY: {draft.get('summary') or ''}",
        f"CLAIM UNDER REVIEW (a restatement of the claim, not the article's view): "
        f"{body.get('beat_1_claim') or ''}",
        f"WHAT THE RESEARCH SHOWS: {body.get('beat_2_evidence') or ''}",
    ]
    for section in body.get("sections") or []:
        parts.append(f"SECTION — {section.get('heading') or ''}: {section.get('body') or ''}")
    parts.append(f"BOTTOM LINE: {body.get('beat_3_bottom_line') or ''}")
    return "\n\n".join(" ".join(part.split()) for part in parts)


def body_text(draft: dict[str, Any]) -> str:
    """The body alone — beats and sections, markers kept — one block per line."""
    body = draft.get("body") or {}
    parts = [body.get("beat_1_claim") or "", body.get("beat_2_evidence") or ""]
    for section in body.get("sections") or []:
        parts.extend([f"## {section.get('heading') or ''}", section.get("body") or ""])
    parts.append(body.get("beat_3_bottom_line") or "")
    return "\n".join(" ".join(part.split()) for part in parts if part)
