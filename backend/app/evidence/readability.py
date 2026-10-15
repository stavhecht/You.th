"""Statistical shorthand a general reader cannot read.

The articles are for people with no science training, and the synthesis prompt
says so ("Writing for everyday readers"). The one part of that a regex can see
is researcher notation copied out of an abstract: "SMD 0.74", "ES = 0.079,
P = 0.39", "95% CI 0.42 to 1.01". Measured 2026-10-15 on the first article
written by Ternary-Bonsai through the real queue (foam rolling and range of
motion), every statistic in it was in that form, unexplained.

This is a **warning**, never a failure, and it is not revisable. The article is
still correct and fully cited; a reviewer can reword the sentence in the desk.
Sending a correct draft back to SYNTHESIZE for a style fix would risk the
revision breaking something that does matter (a rewrite is validated afresh, and
fails for good the second time), and would cost a full synthesis call. If the
rate stays high after the prompt change, measure before promoting it.

Only notation is matched, not ordinary words: "OR" and "MD" alone are a
conjunction and a doctor, so the ratio and difference abbreviations count only
when a number follows them. "CI" is matched anywhere, since nothing else in a
wellness article is written that way.
"""

from __future__ import annotations

import re

from app.domain.contracts import CITATION_MARKER_RE, SynthesisOutput

# A minus sign (\u2212) as well as a hyphen: abstracts use both. Written as an
# escape because ruff's RUF001 rejects the literal glyph.
_NUMBER_AFTER = r"(?=\s*(?:[=<>≤≥:]\s*)?[-\u2212]?\.?\d)"
#: Abbreviations that are notation only when a value follows them.
_ABBREVIATION = re.compile(rf"\b(?:SMD|WMD|MD|ES|OR|RR|HR){_NUMBER_AFTER}")
#: Single-letter statistics and heterogeneity, only with a comparison sign.
_SYMBOL = re.compile(r"(?<![\w.])(?:[dgprP]|I2|I²)\s*[=<>≤≥]\s*[-\u2212]?\.?\d")
#: Confidence intervals, however written.
_INTERVAL = re.compile(r"\bCI\b|\bconfidence intervals?\b", re.IGNORECASE)


def statistical_shorthand(output: SynthesisOutput) -> list[str]:
    """Each distinct piece of notation in the reader-facing text, in order."""
    body = output.body
    texts = [output.headline, output.summary, body.beat_1_claim, body.beat_2_evidence]
    for section in body.sections:
        texts += [section.heading, section.body]
    texts.append(body.beat_3_bottom_line)

    found: list[str] = []
    for text in texts:
        for term in shorthand_in(text):
            if term not in found:
                found.append(term)
    return found


def shorthand_in(text: str) -> list[str]:
    """The notation in one piece of text, in order (repeats included)."""
    plain = CITATION_MARKER_RE.sub("", text)
    matches = [*_ABBREVIATION.finditer(plain), *_SYMBOL.finditer(plain)]
    matches += _INTERVAL.finditer(plain)
    return [_with_value(plain, m) for m in sorted(matches, key=lambda m: m.start())]


def _with_value(text: str, match: re.Match[str]) -> str:
    """The match plus the number after it, so a reviewer can find the sentence."""
    tail = re.match(r"\s*(?:[=<>≤≥:]\s*)?[-\u2212]?\.?\d+(?:\.\d+)?", text[match.end():])
    return (match.group(0) + (tail.group(0) if tail else "")).strip()
