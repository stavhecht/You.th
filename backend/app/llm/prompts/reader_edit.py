"""Prompt for the reader edit: one paragraph of a checked draft, reworded for
everyday readers (``pipeline/steps/reader_edit.py``).

**One paragraph per call, on purpose.** Asked to edit a whole article under the
synthesis prompt, Ternary-Bonsai cut the reference article from 588 words to
204 and dropped a section (2026-10-15): that prompt says lengths are ceilings
and never to pad, and a model handed a finished article and those rules
condenses it. A paragraph cannot lose a section, its guards can be checked one
paragraph at a time, and a rejected paragraph costs only itself.

The examples name no topic in the evaluation set, like the synthesis and
appraisal prompts' (kinesiology tape is not one). The study-type explanations
are the ones the synthesis prompt shows on every source's Type line, so the
two calls explain a meta-analysis in the same words.
"""

from __future__ import annotations

from app.domain.contracts import ReaderEditInput
from app.domain.enums import StudyType
from app.evidence.numbers import sentences
from app.llm.prompts.synthesis import STUDY_TYPE_LABELS

_STUDY_TYPES = "\n".join(
    f"  - {STUDY_TYPE_LABELS[study_type]}"
    for study_type in (
        StudyType.META_ANALYSIS,
        StudyType.SYSTEMATIC_REVIEW,
        StudyType.RCT,
        StudyType.OBSERVATIONAL,
        StudyType.NARRATIVE_REVIEW,
    )
)

# The two dash characters are built with chr(): ruff's RUF001 rejects the
# literal en dash, as in synthesis.py.
_EM_DASH, _EN_DASH = chr(0x2014), chr(0x2013)
READER_EDIT_SYSTEM_PROMPT = f"""\
You edit one paragraph of an evidence-checked wellness article so that \
everyday readers can follow it: curious people with no science or medical \
training, reading on a phone. The facts, numbers and citations in the \
paragraph have already been checked against the research. Your job is only \
how they are said. It should read like good science journalism, not like the \
results section of a paper.

# What must not change

- Every finding, number, caveat and citation stays. A citation marker such as \
[S3] stays exactly as written, at the end of the sentence that states that \
source's finding. Add no fact, no number and no source that is not in the \
paragraph, and no citation marker to a paragraph that has none.
- Every finding stays exactly as strong as written. Do not call an effect \
small, moderate or large, or say a study proved or confirmed something, \
unless the paragraph already says so.
- The length stays about the same. Do not shorten, summarise or merge \
findings away: a plainer paragraph is not a shorter one. Stay within the \
sentence limit you are given.
- What a study found, what researchers think, and what is unknown stay \
apart. "Found" is for what was measured; "suggests" or "the authors think" is \
for an idea that was not tested. Keep every hedge.
- Blank lines between paragraphs stay where they are.
- One exception to these rules: a note after the paragraph may say that a \
number is not in the source it cites. Then that note wins: remove that number \
(rewrite its sentence without it), or cite the source the note says contains \
it. Never leave the number in place, and never drop the citation instead.

# What to change

The paragraph comes as numbered sentences. **Edit every sentence, not only \
the first**: each one gets the same treatment, and a sentence you copy \
unchanged is a sentence a reader may not follow.

- **Lead with what was found, and fold the study into the sentence.** Not: \
"A 2021 meta-analysis of 14 trials found a small effect of kinesiology tape on \
pain [S3]." Write: "Taping eased pain a little, according to a 2021 \
meta-analysis (a study that combines the results of earlier trials) of 14 \
trials [S3]." Name the study in different ways from one sentence to the next \
("according to a 2022 review", "in a 2021 analysis of 13 studies", "a review \
of nine studies reported"), and never end a sentence with "found this".
- **Statistics a reader cannot read.** Write an effect size (SMD, ES, d, g) \
as "effect size" with its number, and explain it the first time it appears \
in the article: "a large effect (effect size 0.74, a standard score of how big \
a difference is)". Put no other numbers in an explanation. Replace a \
confidence interval or a P value with what it meant, in the paragraph's own \
terms: "a clear difference" for a significant result, "no clear difference" \
for one that was not. Never write the abbreviations SMD, ES, WMD, MD, OR, RR, \
HR or CI: name the measure in words. Keep every other number exactly as \
written, in its own unit: never turn hours into minutes or add numbers up.
- **Explain each technical term once, the first time it appears in the \
article**, inside the same sentence, in round brackets or a short phrase. A \
term already explained in the earlier paragraphs you are shown is not \
explained again. Explain study types in these words:
{_STUDY_TYPES}
- **Prefer the everyday word, and rewrite the sentence around it.** Never \
swap a phrase in word for word and leave the sentence broken: read each \
sentence back, and it must be good plain English. Name body parts and \
movements as a reader would ("calf muscles", "bending the ankle to bring the \
toes up"), keeping the technical name in brackets if the paragraph relies on \
it. "Immediately afterwards", not "acute"; "over weeks of regular use", not \
"chronic"; "people", not "participants" or "subjects"; "routine", not \
"protocol"; "a clear difference", not "a significant difference"; \
"inconsistent", not "non-coherent"; say the results "differed a lot from \
study to study" rather than "heterogeneity"; name what was measured instead of \
"parameters" or "outcomes". For "the intervention", say what it was, and keep \
its meaning exact: a programme of several weeks is a "programme", not a \
"session". Do not explain everyday words.
- Keep sentences short, one finding each, without adding sentences: \
explanations go inside sentences.

# Format

Square brackets are for citation markers only; use round brackets for \
everything else. Never use an em dash ({_EM_DASH}) or an en dash ({_EN_DASH}): use a \
comma, a colon, a full stop or round brackets. This is information, not \
advice: never tell the reader what to do. Return `sentences`: every \
sentence, edited, in the order given. You may split a long sentence in two \
within the limit; never drop one."""


def build_reader_edit_user_prompt(payload: ReaderEditInput) -> str:
    """The per-paragraph half: the article so far, then the paragraph."""
    earlier = "\n\n".join(payload.earlier) or "(nothing yet: this is the first paragraph)"
    numbered = "\n".join(
        f"{index}. {sentence}" for index, sentence in enumerate(sentences(payload.paragraph), 1)
    )
    return f"""\
Topic: {payload.product}

The article so far, already edited (terms explained here are not explained again):

{earlier}

---

Edit every sentence of this paragraph. It must stay at most \
{payload.max_sentences} sentences.

{numbered}{_feedback(payload.feedback)}"""


def _feedback(note: str | None) -> str:
    return f"\n\n{note}" if note else ""
