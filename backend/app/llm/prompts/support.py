"""Prompt for VERIFY: does each cited paper say what the article says it says.

One call per cited paper, with only the article sentences that cite it: one
abstract is a short prompt for a small model, and judging a sentence against
the one paper it names is a narrower task than judging it against twelve.
The lexical checks in VALIDATE (shared words, numbers present) cannot see a
sentence that uses a paper's words for a claim the paper does not make; this
reads the meaning. It warns; it never changes a draft or fails a run.

**No worked example names a claim in the evaluation set**, for the reason the
appraisal prompt gives.
"""

from __future__ import annotations

from app.domain.contracts import SupportInput

SUPPORT_SYSTEM_PROMPT = """\
You check an evidence-based article against one of the papers it cites. You are \
given the paper's title and abstract, and numbered sentences from the article \
that cite this paper. For each sentence, decide whether the paper says what the \
sentence says.

For each sentence:

1. `quote`: copy, word for word, the sentence from the paper that states what \
the article sentence says. If the paper states no such thing, write none. Do \
not quote a sentence that only mentions the same topic.
2. `verdict`:
   - supported: the paper states it. Plainer wording is fine; the meaning and \
any number must match.
   - partly: the paper states part of it, and the article sentence adds \
something the paper does not say: a number, a group of people, a cause, a \
stronger word ("proves", "large", "all"), or a result for a different outcome.
   - not_supported: the paper does not state it, or says the opposite.

A sentence about the evidence itself (how many studies there are, what kind, \
what has not been studied) is supported if this paper is one of the studies it \
describes: "the evidence is one small trial" is supported by that trial.

Judge only against the text given. A sentence that is true but not stated in \
this paper is not_supported here. If the quote is none, the verdict is \
not_supported.

Example. Paper: "In 48 older adults, eight weeks of balance training reduced \
falls by a third compared with stretching." Sentence: "Balance training cut \
falls in older adults." -> supported. Sentence: "Balance training cut falls and \
improved bone density." -> partly (bone density is not reported). Sentence: \
"Balance training prevents fractures." -> not_supported.

Give one item for every numbered sentence.
"""


def build_support_user_prompt(payload: SupportInput) -> str:
    numbered = "\n".join(
        f"{number}. {sentence}" for number, sentence in enumerate(payload.sentences, start=1)
    )
    return (
        f"Paper {payload.handle}: {payload.title}\n\n"
        f"Abstract:\n{payload.abstract}\n\n"
        f"Article sentences that cite it:\n{numbered}\n"
    )
