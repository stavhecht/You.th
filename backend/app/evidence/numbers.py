"""A number written beside a citation must be in the source it cites.

The commonest grounding failure the evaluation found was not invention but
attribution: a real statistic from one source, cited to another. Measured
2026-10-01 over 113 articles, 14% quoted such a number, and 71 of 76 flagged
numbers appeared in a different source the model had been shown — "effect size
0.26 (0.15 to 0.38) [S8]" where the figures are S7's. The handle exists, so
the existing checks pass the draft, and the reader is sent to a paper that does
not say what the sentence says.

So: every sentence that cites something is read for its numbers, and each must
appear — exactly, or as a rounding — in the title, year, abstract or full-text
excerpts of the sources that sentence cites. That is exactly the text the model
was shown for them. Two things are not checked:

* **Counts of the sources themselves** ("three trials", "2 meta-analyses"): the
  model counting its citations, not quoting a paper.
* **Uncited sentences**, which have no source to check against. Whether a
  finding *should* be cited is the cited-beat rule's question, not this one's.

A failure names the numbers, the handles, and — when it can — which source does
contain them, because that is the fix: re-cite, not rewrite.
"""

from __future__ import annotations

import re

from app.domain.contracts import (
    CITATION_MARKER_RE,
    PromptSource,
    SynthesisOutput,
    extract_handles,
)

_NUMBER = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)")
#: A number followed, within two words, by one of these is a count of sources.
_COUNT_NOUN = re.compile(
    r"^\s*(?:[\w-]+\s){0,2}?(trials?|stud(?:y|ies)|reviews?|meta-analys[ie]s|sources|papers|"
    r"RCTs?|articles|analyses)\b",
    re.IGNORECASE,
)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")
_ABBREVIATION = re.compile(r"\b(e\.g|i\.e|et al|vs|approx|ca|cf|Fig|No)\.", re.IGNORECASE)
_PLACEHOLDER = "\u2024"  # stands in for an abbreviation's full stop while splitting
_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "hundred": 100,
}  # fmt: skip
#: Compound numbers abstracts spell out at the start of a sentence: "Twenty-six
#: trials with high methodological quality". Read only as 20 and 6, a correct
#: "26 high-quality trials" failed as unsourced (measured 2026-10-15).
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
         "eighty": 80, "ninety": 90}  # fmt: skip
_UNITS = ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine")
_COMPOUND = re.compile(rf"\b({'|'.join(_TENS)})[-\s]({'|'.join(_UNITS)})\b")


#: A decimal written without its leading zero (".005"), as abstracts often
#: write p-values. Read as "0.005", or "p = .005" in a source would never match
#: "p = 0.005" in a sentence — measured, it failed a correctly cited draft.
_BARE_DECIMAL = re.compile(r"(?<![\w.])\.(\d)")


def _normalise(text: str) -> str:
    return _BARE_DECIMAL.sub(r"0.\1", text)


def _value(raw: str) -> float:
    return float(raw.replace(",", ""))


def source_numbers(text: str) -> list[float]:
    """Every number in a source's text, digits or a small spelled-out word."""
    found = [_value(match.group(1)) for match in _NUMBER.finditer(_normalise(text))]
    lowered = text.lower()
    found.extend(value for word, value in _WORDS.items() if re.search(rf"\b{word}\b", lowered))
    found.extend(_TENS[t] + _UNITS.index(u) + 1 for t, u in _COMPOUND.findall(lowered))
    return found


def checked_numbers(sentence: str) -> list[str]:
    """The numbers in a sentence a source should contain."""
    numbers: list[str] = []
    sentence = _normalise(sentence)
    for match in _NUMBER.finditer(sentence):
        value = _value(match.group(1))
        if value <= 50 and value.is_integer() and _COUNT_NOUN.match(sentence[match.end() :]):
            continue
        numbers.append(match.group(1))
    return numbers


def is_present(raw: str, numbers: list[float]) -> bool:
    """Exactly, or as a rounding of a source value (27.9 written as 28)."""
    value = _value(raw)
    return any(
        candidate == value
        or (value.is_integer() and abs(candidate - value) < 0.5)
        or round(candidate, 1) == value
        for candidate in numbers
    )


def sentences(text: str) -> list[str]:
    """Sentences, each keeping the markers that close it.

    A marker run that opens a sentence belongs to the one before it
    ("… fell. [S1] Another …"), unless a lowercase word follows it, which makes
    it that sentence's subject ("[S6] also found …").
    """
    protected = _ABBREVIATION.sub(lambda m: m.group(0).replace(".", _PLACEHOLDER), text)
    out: list[str] = []
    for piece in (p.strip() for p in _SENTENCE_END.split(protected)):
        piece = piece.replace(_PLACEHOLDER, ".")
        match = re.match(rf"^((?:{CITATION_MARKER_RE.pattern}\s*)+)", piece)
        rest = piece[match.end() :].lstrip() if match else ""
        if match and out and not rest[:1].islower():
            out[-1] = f"{out[-1]} {match.group(1).strip()}"
            piece = rest
        if piece:
            out.append(piece)
    return out


def source_text(source: PromptSource) -> str:
    """What the model was shown for one source."""
    excerpts = " ".join(excerpt.text for excerpt in source.excerpts)
    return f"{source.title} {source.year or ''} {source.abstract} {excerpts}"


def unsupported(
    output: SynthesisOutput, sources: list[PromptSource]
) -> list[tuple[str, list[str], list[str], list[str]]]:
    """``(sentence, handles, missing numbers, handles that do contain them)``."""
    body = output.body
    blocks = [body.beat_1_claim, body.beat_2_evidence]
    blocks += [section.body for section in body.sections]
    blocks.append(body.beat_3_bottom_line)
    return unsupported_in(blocks, sources)


def unsupported_in(
    blocks: list[str], sources: list[PromptSource]
) -> list[tuple[str, list[str], list[str], list[str]]]:
    """``unsupported`` over any blocks of prose: one paragraph, for the reader edit."""
    by_handle = {source.handle: source for source in sources}
    numbers_by_handle = {h: source_numbers(source_text(s)) for h, s in by_handle.items()}
    problems = []
    for block in blocks:
        for sentence in sentences(block):
            handles = sorted(
                extract_handles(sentence) & by_handle.keys(), key=lambda h: int(h[1:])
            )
            if not handles:
                continue
            plain = CITATION_MARKER_RE.sub("", sentence)
            cited = [n for h in handles for n in numbers_by_handle[h]]
            absent = (raw for raw in checked_numbers(plain) if not is_present(raw, cited))
            missing = list(dict.fromkeys(absent))
            if not missing:
                continue
            elsewhere = sorted(
                (
                    h
                    for h, numbers in numbers_by_handle.items()
                    if h not in handles and all(is_present(raw, numbers) for raw in missing)
                ),
                key=lambda h: int(h[1:]),
            )
            problems.append((" ".join(plain.split()), handles, missing, elsewhere))
    return problems
