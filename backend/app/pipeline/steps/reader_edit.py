"""The reader edit: a finished draft reworded for everyday readers, one
paragraph at a time, each paragraph kept only if provably nothing but its
wording changed.

**Why a pass, and not just the prompt.** The synthesis prompt has a "Writing
for everyday readers" section. Measured 2026-10-15 on the reference article
(foam rolling and range of motion, Ternary-Bonsai, real queue): with that
section alone, statistical shorthand went from 11 pieces to 0, but 15 of 19
sentences still opened "A 2022 meta-analysis found", dorsiflexion, triceps
surae, "chronic" and "heterogeneity" went unexplained, the effect sizes were
dropped rather than explained, and Flesch reading ease did not move (35, then
31). Writing a grounded article under some seventy rules and writing it plainly
are two jobs, and a local model doing both at once does the first.

**Why a paragraph at a time.** Asked to edit the whole article under the
synthesis prompt, the same model cut it from 588 words to 204 and dropped a
section — see ``prompts/reader_edit.py``. A paragraph cannot lose a section, and
a rejected paragraph costs only itself.

**A paragraph is kept only when ``paragraph_rejection`` finds nothing**, and
otherwise the original stays, exactly as if the pass had not run. Same rule as
the coverage re-prompt: a step that improves an article must never be able to
fail one. What a rewording must not change: which sources the paragraph cites,
its numbers (none added, none moved beside a source that lacks them), its
sentence limit, and its length (no more than 15% shorter, for the confidence
intervals and P values it is asked to take out: a plainer article, not a
shorter one). The assembled article is then checked whole
(``article_rejection``) and VALIDATE still checks it in full; the guards only
make it unlikely the edit hands VALIDATE anything new to find.

The headline is not edited: it is already plain, and twelve words leave no room
to explain anything. The summary is, but is not shown to later paragraphs as
"already explained": it is read apart from the body (the feed card), so the
body explains its own terms.

**An explanation is given once, and that is enforced here, not asked for.**
Shown the article so far, the model still explained "meta-analysis" at the top
of every paragraph (four times in the reference article).
``drop_repeated_explanations`` removes a bracketed explanation the body has
already given. It never removes one carrying a number, so an effect size stays.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.domain.contracts import (
    CITATION_MARKER_RE,
    PromptSource,
    SynthesisInput,
    SynthesisOutput,
    count_sentences,
    extract_handles,
)
from app.evidence import numbers
from app.evidence.readability import shorthand_in, statistical_shorthand
from app.evidence.validation import (
    check_beats_are_cited,
    check_body_parses,
    misattributed_sentences,
)

#: An edit may shorten a paragraph by this much, for the confidence intervals and
#: P values it is asked to take out, and no more.
MIN_LENGTH_RATIO = 0.85

#: Sentence limits, as the validators in ``domain/contracts.py`` enforce them.
#: Restated so the model can be told its paragraph's limit; ``assemble``
#: re-validates, so a drift here costs an edit, never a broken article.
SUMMARY_SENTENCES, BEAT_1_SENTENCES, BEAT_2_SENTENCES = 2, 4, 5
SECTION_SENTENCES, BEAT_3_SENTENCES = 8, 5

#: Opening words of a sentence that leads with its study, not its finding.
_STUDY_LED = re.compile(
    r"^\s*(?:A|An|The|One|Another|This|That)\s+(?:\w+\s+){0,3}?"
    r"(?:meta-analys[ie]s|systematic review|review|trial|study|analysis)\b",
    re.IGNORECASE,
)


#: Words that state how big or how certain a finding is. An edit may not bring
#: one into a paragraph that did not use it: measured on the reference article,
#: the edit called an effect size of 0.53 "moderate" where neither the draft nor
#: the source said so. Rewording may carry a judgement over, never add one.
#: ("big" is left out: the effect-size explanation the prompt teaches uses it.)
STRENGTH_WORDS = frozenset(
    {
        "small", "slight", "modest", "moderate", "medium", "large", "huge", "substantial",
        "strong", "weak", "trivial", "negligible", "major", "minor",
        "prove", "proves", "proved", "proven", "confirm", "confirms", "confirmed",
        "definitely", "certainly", "conclusively", "guarantee", "guarantees",
    }
)  # fmt: skip

#: A bracketed aside, and the comma-separated phrases inside one.
_ASIDE = re.compile(r"\s*\(([^()]*)\)")
#: An explanation shorter than this is a word or a label, not worth removing.
_MIN_EXPLANATION_WORDS = 4


@dataclass(frozen=True, slots=True)
class Part:
    """One paragraph of a draft, in reading order.

    ``slot`` is the field it came from; a section written as several
    blank-line-separated paragraphs gives one part per paragraph, each limited
    to what the section's eight sentences leave after the others.
    """

    name: str
    text: str
    max_sentences: int
    slot: str


def parts(draft: SynthesisOutput) -> list[Part]:
    body = draft.body
    out = [
        Part("summary", draft.summary, SUMMARY_SENTENCES, "summary"),
        Part("beat 1", body.beat_1_claim, BEAT_1_SENTENCES, "beat_1"),
        Part("beat 2", body.beat_2_evidence, BEAT_2_SENTENCES, "beat_2"),
    ]
    for index, section in enumerate(body.sections):
        paragraphs = [p.strip() for p in section.body.split("\n\n") if p.strip()]
        total = count_sentences(section.body)
        for number, paragraph in enumerate(paragraphs, 1):
            name = f'section "{section.heading}"'
            if len(paragraphs) > 1:
                name += f" paragraph {number}"
            room = SECTION_SENTENCES - (total - count_sentences(paragraph))
            out.append(Part(name, paragraph, room, f"section_{index}"))
    out.append(Part("beat 3", body.beat_3_bottom_line, BEAT_3_SENTENCES, "beat_3"))
    return out


def explanations(text: str) -> set[str]:
    """The explaining phrases in a text's brackets, normalised for comparison."""
    return {
        _normalise(phrase)
        for aside in _ASIDE.findall(text)
        for phrase in aside.split(",")
        if _is_explanation(phrase)
    }


def drop_repeated_explanations(text: str, seen: set[str]) -> str:
    """``text`` without the bracketed explanations already in ``seen``, or
    given earlier in ``text`` itself.

    A bracket keeps any phrase that is new, short, or carries a number, and goes
    altogether when nothing is left: "(effect size 0.823, a standard score of
    how big a difference is)" becomes "(effect size 0.823)" the second time.
    """
    given = set(seen)

    def keep(match: re.Match[str]) -> str:
        phrases = match.group(1).split(",")
        kept = [
            p for p in phrases if not (_is_explanation(p) and _normalise(p) in given)
        ]
        given.update(_normalise(p) for p in kept if _is_explanation(p))
        if len(kept) == len(phrases):
            return match.group(0)
        if not kept:
            return ""
        return f" ({','.join(kept).strip()})"

    return _ASIDE.sub(keep, text)


def _is_explanation(phrase: str) -> bool:
    return len(phrase.split()) >= _MIN_EXPLANATION_WORDS and not re.search(r"\d", phrase)


def _normalise(phrase: str) -> str:
    return " ".join(phrase.lower().split())


def paragraph_rejection(
    part: Part, edited: str, sources: list[PromptSource], *, same_handles: bool = True
) -> str | None:
    """Why ``edited`` must not replace ``part.text``, or None to keep it.

    ``same_handles=False`` is for a repair, whose own check decides which
    citations may change.
    """
    if same_handles and extract_handles(edited) != extract_handles(part.text):
        return (
            "changed which sources it cites: keep every citation marker exactly as in the "
            "draft, and add none to a paragraph that has none"
        )
    unmarked = CITATION_MARKER_RE.sub("", edited)
    if "[" in unmarked or "]" in unmarked:
        return "put square brackets around something other than a citation"
    kept = numbers.source_numbers(part.text)
    added = [raw for raw in _numbers(edited) if not numbers.is_present(raw, kept)]
    if added:
        return f"added numbers not in the draft: {', '.join(dict.fromkeys(added))}"
    if new := _unsourced([edited], sources) - _unsourced([part.text], sources):
        number, handles = sorted(new)[0]
        return (
            f"put {number} beside {', '.join(handles)}, which does not contain it: keep each "
            "number in the sentence that cites the source it comes from"
        )
    stronger = sorted(_strength(edited) - _strength(part.text))
    if stronger:
        return (
            f"added the word {', '.join(stronger)}, which the draft did not use: state each "
            "finding exactly as strongly as the draft does, and give an effect size as a "
            "number with no size word unless the draft has one"
        )
    if (count := count_sentences(edited)) > part.max_sentences:
        return f"ran to {count} sentences, over its limit of {part.max_sentences}"
    before, after = _words(part.text), _words(edited)
    if after < before * MIN_LENGTH_RATIO:
        return f"shortened it from {before} to {after} words"
    return None


def repair_rejection(
    part: Part, edited: str, sources: list[PromptSource], named: set[str], failed: set[str]
) -> str | None:
    """Why a repair of ``part`` cannot be kept, or None to keep it.

    ``named`` holds the handles the failures pointed at (the ones cited and the
    ones holding the number): a fix may move a citation among those and touch
    no other. ``failed`` holds the numbers that failed: none may stay in a
    sentence that cites nothing, since the number check reads only cited
    sentences and dropping the marker would pass it. Then every edit guard,
    and no number left beside a source that does not contain it.
    """
    if changed := (extract_handles(edited) ^ extract_handles(part.text)) - named:
        return (
            f"changed citations the problem did not name ({', '.join(sorted(changed))}): "
            "keep every other citation marker exactly as it is"
        )
    for sentence in numbers.sentences(edited):
        if extract_handles(sentence):
            continue
        kept = numbers.source_numbers(sentence)
        if left := sorted(raw for raw in failed if numbers.is_present(raw, kept)):
            return (
                f"kept {', '.join(left)} in a sentence that cites nothing: remove the "
                "number, or cite the source that contains it"
            )
    if left := _unsourced([edited], sources):
        number, handles = sorted(left)[0]
        return f"still gives {number} beside {', '.join(handles)}, which does not contain it"
    # The final round only warns on this, so a repair is the last place it can be refused.
    if wrong := misattributed_sentences([edited], sources):
        detail = wrong[0].detail
        return (
            f"cites {', '.join(detail['handles'])} for “{detail['sentence']}”, but that "
            f"finding is worded like {', '.join(detail['found_in'])}: "
            "cite the source that reports it"
        )
    return paragraph_rejection(part, edited, sources, same_handles=False)


def assemble(draft: SynthesisOutput, texts: list[str]) -> SynthesisOutput | None:
    """The draft with ``texts`` in place of its parts, or None if that breaks
    the output contract (re-validated here, since ``model_copy`` would not)."""
    slots: dict[str, list[str]] = {}
    for part, text in zip(parts(draft), texts, strict=True):
        slots.setdefault(part.slot, []).append(text)
    candidate = draft.model_dump()
    candidate["summary"] = slots["summary"][0]
    candidate["body"] = {
        "beat_1_claim": slots["beat_1"][0],
        "beat_2_evidence": slots["beat_2"][0],
        "sections": [
            {"heading": s.heading, "body": "\n\n".join(slots[f"section_{index}"])}
            for index, s in enumerate(draft.body.sections)
        ],
        "beat_3_bottom_line": slots["beat_3"][0],
    }
    try:
        return SynthesisOutput.model_validate(candidate)
    except ValueError:
        return None


def article_rejection(
    original: SynthesisOutput, edited: SynthesisOutput, payload: SynthesisInput
) -> str | None:
    """The whole-article checks no single paragraph can see."""
    if {f.code for f in check_beats_are_cited(edited)} - {
        f.code for f in check_beats_are_cited(original)
    }:
        return "left a beat or section without a citation"
    if check_body_parses(edited) and not check_body_parses(original):
        return "broke the body's format"
    before = _unsourced(_body(original), payload.sources)
    if _unsourced(_body(edited), payload.sources) - before:
        return "moved a number beside a source that does not contain it"
    return None


def leftovers(text: str) -> str | None:
    """What a kept edit still leaves for a reader, as feedback naming the
    sentences, or None when nothing is left to fix."""
    study_led, shorthand = [], []
    for index, sentence in enumerate(numbers.sentences(text), 1):
        plain = CITATION_MARKER_RE.sub("", sentence)
        if _STUDY_LED.match(plain):
            study_led.append(str(index))
        if terms := shorthand_in(plain):
            shorthand.append(f"sentence {index} ({'; '.join(terms)})")
    notes = []
    if study_led:
        notes.append(
            f"sentence{'s' if len(study_led) > 1 else ''} {', '.join(study_led)} still "
            "open with the study (\"A 2022 meta-analysis found\"): lead each with what was "
            "found and fold the study into the sentence (\"according to a 2022 review\")"
        )
    if shorthand:
        notes.append(
            f"statistical shorthand is left in {', '.join(shorthand)}: write an effect size "
            "as \"effect size\" with its number, and replace confidence intervals and P "
            "values with what they meant"
        )
    return "; ".join(notes) or None


def score(text: str) -> int:
    """Lower is plainer: the problems ``leftovers`` would name."""
    plain = [CITATION_MARKER_RE.sub("", s) for s in numbers.sentences(text)]
    return sum(bool(_STUDY_LED.match(s)) for s in plain) + sum(
        len(shorthand_in(s)) for s in plain
    )


def readability(draft: SynthesisOutput) -> dict[str, float | int]:
    """What the edit is for, measured on one draft, for the stage metrics."""
    sentences = [
        CITATION_MARKER_RE.sub("", s)
        for part in parts(draft)[1:]
        for s in numbers.sentences(part.text)
    ]
    words = sum(len(s.split()) for s in sentences)
    return {
        "words": _words(draft.body.as_text()),
        "sentences": len(sentences),
        "words_per_sentence": round(words / len(sentences), 1) if sentences else 0,
        "study_led_sentences": sum(bool(_STUDY_LED.match(s)) for s in sentences),
        "statistical_shorthand": len(statistical_shorthand(draft)),
    }


def _unsourced(
    blocks: list[str], sources: list[PromptSource]
) -> set[tuple[str, tuple[str, ...]]]:
    """Each (number, cited handles) pair the number check would flag.

    Compared as sets, not counts: an edit that fixes one misattributed number
    and makes another leaves the count unchanged and is still a new error.
    """
    return {
        (number, tuple(handles))
        for _, handles, missing, _ in numbers.unsupported_in(blocks, sources)
        for number in missing
    }


def _body(draft: SynthesisOutput) -> list[str]:
    return [p.text for p in parts(draft)[1:]]


def _strength(text: str) -> set[str]:
    return set(re.findall(r"[a-z]+", text.lower())) & STRENGTH_WORDS


def _numbers(text: str) -> list[str]:
    plain = CITATION_MARKER_RE.sub("", text)
    return [raw for s in numbers.sentences(plain) for raw in numbers.checked_numbers(s)]


def _words(text: str) -> int:
    return len(CITATION_MARKER_RE.sub("", text).split())
