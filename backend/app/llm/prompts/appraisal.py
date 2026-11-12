"""Prompt templates for APPRAISE — which way each source points.

The verdict scale measures support, and until APPRAISE existed nothing in the
pipeline recorded whether a cited paper *found* what a claim says or tested it
and found nothing. Retrieval is direction-neutral (<substance> AND <outcome>),
so null trials reach the prompt; this is where they are told apart.

Two modes (APPRAISAL_MODE), both putting relevance before direction:

* one_call — APPRAISAL_SYSTEM_PROMPT: the model names the claim's
  outcome, quotes each abstract's sentence about it (or "none") and labels the
  quote. The default.
* two_call — RELEVANCE_SYSTEM_PROMPT over every source, then
  DIRECTION_SYSTEM_PROMPT over only the sources relevance accepted.

Measured 2026-10-02 on llama3.1:8b over 132 blind-labelled pairs: direction
alone flipped nothing but called only 23% of off-topic studies off topic; one
call with free-text summaries caught 72% and flipped five; the quote design
(one call) got 79% right, flipped two, caught 75%; the two-call design got 78%,
flipped two, caught 69%. The table and the reasoning are in app/pipeline/CLAUDE.md.

Both are deliberately narrow calls rather than fields the synthesis model fills
in: labelling twelve abstracts is a far easier task for a small model than
writing an article and keeping its citations straight at once, and a label the
article's own author assigned could only show the model agreeing with itself.

**No worked example names a claim in the evaluation set.** The first version
used creatine-and-kidneys, which is case adv_010, and scored it against the
prompt's own answer.

The system prompts are stable across every call; the per-claim block is
volatile and goes after them.
"""

from __future__ import annotations

from app.domain.contracts import AppraisalInput
from app.llm.prompts.synthesis import _round

APPRAISAL_SYSTEM_PROMPT = """\
You label scientific abstracts for an evidence-checking service. You are given \
one claim about a wellness product or practice, and the abstracts of studies \
retrieved for that claim. For each study, find what it reports about the \
claim's outcome, and say which way that result points.

First, write `claim_outcome`: what the claim says changes, in a few words. Name \
the change, not just the topic. For "prevents headaches" it is how often \
headaches happen, not how bad or how long they are. For "improves memory" it is \
scores on memory tests.

Then, for each study:

1. `quote`: copy, word for word, the sentence from the abstract that reports a \
result for claim_outcome. Take it from the results or conclusions, never from \
the background or objectives, which say what a study set out to test, not what \
it found. If no sentence reports a result for claim_outcome itself, write none. \
A sentence about a related outcome does not count: for "prevents headaches", a \
sentence about how long headaches last is not a result for claim_outcome. A \
text that reports no results at all, such as a product description, an \
advertisement or a study plan, has none.
2. `reports_claim_outcome`: true only if your quote reports a result for \
claim_outcome itself. false if the quote is none.
3. `stance`: the label for what your quote reports.

Labels:

- supports: the quote reports the effect the claim describes, in the direction \
the claim describes.
- no_effect: the quote reports no meaningful or no statistically significant \
difference.
- contradicts: the quote reports the opposite of the claim.
- unclear: the quote reports a split or uncertain result, or says the evidence \
is not enough to tell.
- off_topic: the quote is none.

Rules:

1. Label from the abstract only. Do not use what you know about the substance \
from anywhere else.

2. Label against the claim exactly as it is written. "supports" means the result \
bears the claim out, never that the result is good news.
- If the claim says something causes harm ("raises blood pressure"), a study \
that found no rise is no_effect and a study that found the rise is supports.
- If the claim itself says there is no effect ("does not affect weight"), a \
study that found no effect supports it, and a study that found any effect, good \
or bad, contradicts it.

3. Judge the result, not the authors' tone. A conclusion that something "may be \
beneficial", written over results showing no significant difference, is \
no_effect.

4. A review or meta-analysis is labelled by its overall result for \
claim_outcome across the studies it pooled.

5. Animal and cell studies are labelled by their result like any other. How \
much they count is decided elsewhere, not by you.

6. Give exactly one item for every handle listed, using the handle exactly as \
written. Do not skip one and do not invent one.

Return only the structured object. No commentary."""


def build_appraisal_user_prompt(payload: AppraisalInput) -> str:
    """Render the volatile, per-claim half of the appraisal prompt.

    Square brackets in source text are made round for the same reason the
    synthesis prompt does it (``synthesis._round``): here a bracket is a
    handle, and a copied confidence interval is noise beside one.
    """
    handles = ", ".join(source.handle for source in payload.sources)
    blocks = "\n\n---\n\n".join(
        f"[{source.handle}] {_round(source.title)}\n"
        f"Type: {str(source.study_type).replace('_', ' ')}\n"
        f"Abstract: {_round(source.abstract)}"
        for source in payload.sources
    )
    return f"""\
Product or trend: {payload.product}

Claim: {payload.claim}

Sources ({len(payload.sources)}; label each of {handles}):

{blocks}

---

Write claim_outcome, then quote and label every source above for the claim: \
{payload.claim}"""


# --- APPRAISAL_MODE=two_call -------------------------------------------------

RELEVANCE_SYSTEM_PROMPT = """\
You check scientific abstracts for an evidence-checking service. You are given \
one claim about a wellness product or practice, and the abstracts of studies \
retrieved for that claim. For each study, say whether it measured what the \
claim is about. You do not judge whether the claim is true.

First, write `claim_outcome`: the one outcome the claim is about, in a few \
words. For "improves memory" it is memory. For "shortens recovery after \
surgery" it is recovery time after surgery.

Then, for each study:

1. `outcome_measured`: what the study actually measured, in a few words, read \
from its methods and results.
2. `measures_claim_outcome`: true only if the study measured the claim's own \
outcome. A related outcome is false: for "improves memory", a study of mood, \
attention or brain scans did not measure memory. A different condition or a \
different substance is false. A text that reports no result at all, such as a \
product description, an advertisement, a study plan or a list of questions, is \
false.

Rules:

1. Read from the abstract only. Do not use what you know about the substance \
from anywhere else.

2. Give exactly one item for every handle listed, using the handle exactly as \
written. Do not skip one and do not invent one.

Return only the structured object. No commentary."""

DIRECTION_SYSTEM_PROMPT = """\
You label scientific abstracts for an evidence-checking service. You are given \
one claim about a wellness product or practice, and the abstracts of studies \
retrieved for that claim. For each study, say which way its reported result \
points for that claim.

Labels:

- supports: the study reports the effect the claim describes, in the direction \
the claim describes. Claim "improves memory", result "memory test scores \
improved compared with placebo".
- no_effect: the study tested the claim's outcome and found no meaningful or \
no statistically significant difference.
- contradicts: the study found the opposite of the claim: the outcome got \
worse, or it found harm where the claim promises a benefit.
- unclear: the abstract states no result for this outcome, or the result is \
split within the study (some measures improved and others did not), or a \
review says the evidence is not enough to tell.
- off_topic: the study does not test this claim at all, for example a \
different outcome, a different substance, or only a description of methods.

Rules:

1. Label from the abstract only. Do not use what you know about the substance \
from anywhere else. A study you have heard of is labelled by what this \
abstract says.

2. Label against the claim exactly as it is written. If the claim says \
something causes harm ("raises blood pressure"), a study that found no rise is \
no_effect and a study that found the rise is supports. "supports" means the \
result bears the claim out, never that the result is good news.

3. Judge the result, not the authors' tone. A conclusion that something "may \
be beneficial", written over results showing no significant difference, is \
no_effect.

4. A review or meta-analysis is labelled by its overall result across the \
studies it pooled.

5. Animal and cell studies are labelled by their result like any other. How \
much they count is decided elsewhere, not by you.

6. Give exactly one item for every handle listed, using the handle exactly as \
written. Do not skip one and do not invent one.

7. For each item, write `finding` first: one short sentence saying what the \
abstract reports for the claim's outcome. Then choose the label that matches \
that sentence.

Return only the structured object. No commentary."""


def _blocks(payload: AppraisalInput) -> str:
    """The sources as both calls show them.

    Square brackets in source text are made round for the same reason the
    synthesis prompt does it (``synthesis._round``): here a bracket is a
    handle, and a copied confidence interval is noise beside one.
    """
    return "\n\n---\n\n".join(
        f"[{source.handle}] {_round(source.title)}\n"
        f"Type: {str(source.study_type).replace('_', ' ')}\n"
        f"Abstract: {_round(source.abstract)}"
        for source in payload.sources
    )


def _handles(payload: AppraisalInput) -> str:
    return ", ".join(source.handle for source in payload.sources)


def build_relevance_user_prompt(payload: AppraisalInput) -> str:
    """The per-claim half of the relevance call: every ranked source."""
    return f"""\
Product or trend: {payload.product}

Claim: {payload.claim}

Sources ({len(payload.sources)}; check each of {_handles(payload)}):

{_blocks(payload)}

---

Write claim_outcome, then check every source above for whether it measured it. \
The claim: {payload.claim}"""


def build_direction_user_prompt(payload: AppraisalInput) -> str:
    """The per-claim half of the direction call: only the sources the relevance
    call accepted, so the model is never asked for a direction it should not give."""
    return f"""\
Product or trend: {payload.product}

Claim: {payload.claim}

Sources ({len(payload.sources)}; label each of {_handles(payload)}):

{_blocks(payload)}

---

Label every source above against the claim: {payload.claim}"""
