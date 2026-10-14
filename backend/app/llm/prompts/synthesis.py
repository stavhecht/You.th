"""Prompt template for LLM call 2 — the RAG generation.

This is the load-bearing prompt. It encodes invariants #2 (grounded), #3
(evidence-proportional) and #4 (not medical advice, no brand attacks) from
DESIGN.md as instructions — but note that none of them are *trusted* here.
Every one is re-checked deterministically in ``evidence/validation.py`` after
generation, against the prompt's handle set and the database. The prompt's job
is to make the model succeed; validation's job is to catch it when it doesn't.

Ordering note: the system prompt is byte-stable across every article and
carries the cache_control breakpoint. The source block is volatile and is
rendered into the user turn, after it.
"""

from __future__ import annotations

from app.domain.contracts import SynthesisInput
from app.domain.enums import StudyType

#: Human-readable labels for study types, used when rendering sources into the
#: prompt. The model sees the same vocabulary the verdict cap is expressed in.
#:
#: **Each carries its own plain-English gloss, and that is load-bearing rather
#: than decorative.** The Framing section tells the model to explain every
#: technical term the first time it appears, and measured over six runs it did
#: that for "RCT" and ignored it for "meta-analysis" and "systematic review" -
#: the two terms it meets most often, because they are printed on every source
#: line. Instructing against the model's own input did not work; showing it the
#: explained form in that input did. Same lever as removing the em dashes from
#: this file: a local model mimics the register it is handed far more reliably
#: than it follows a rule about it.
#:
#: Keep these short enough to sit inside a sentence, and phrased for someone
#: with no medical training. A gloss the model cannot reuse verbatim is a gloss
#: it will drop.
STUDY_TYPE_LABELS: dict[StudyType, str] = {
    StudyType.META_ANALYSIS: (
        "meta-analysis (a study that pools the numbers from several earlier "
        "trials to get one overall result)"
    ),
    StudyType.SYSTEMATIC_REVIEW: (
        "systematic review (a search for every study on a question, assessed "
        "together to a set method)"
    ),
    StudyType.RCT: (
        "randomised controlled trial (people are put into groups at random, so "
        "the groups can be compared fairly)"
    ),
    StudyType.OBSERVATIONAL: (
        "observational study (researchers recorded what people already did, "
        "rather than assigning a treatment)"
    ),
    StudyType.NARRATIVE_REVIEW: (
        "narrative review (a summary of other studies, with no stated method "
        "for finding them)"
    ),
    StudyType.CASE_REPORT: "case report (a write-up of one or a few patients)",
    StudyType.ANIMAL: "animal study (done in animals, not in people)",
    StudyType.IN_VITRO: "in-vitro study (done on cells in a dish, not in people)",
    StudyType.UNKNOWN: "study type unclear",
}


# The two dash characters in the "never use an em dash" rule are written as
# \u escapes because ruff's RUF001 rejects a literal en dash as an ambiguous
# character. Python resolves them at parse time, so the model sees the glyphs.
#
# The opening line called these "short" articles until 2026-09-29. It was the
# first thing the model read on every call, and the same kind of brevity cue
# that cost sections when it reached the model through the contracts'
# docstrings (see CLAUDE.md). Length is still governed by the evidence rules
# under "Structure and length", which are unchanged. The effect of dropping the
# word is not measured yet.
SYNTHESIS_SYSTEM_PROMPT = """\
You write evidence-checked articles about wellness products and trends for \
a general audience. Each article states what something claims, works \
through what the research actually shows, and gives an honest bottom line.

# Grounding: the absolute constraint

Write only from the sources provided in the user message. Every source \
carries a handle (S1, S2, …).

- Attach the handle of the supporting source to every factual claim you \
make, inline, in square brackets: "one trial found lower evening cortisol \
[S1]".
- When several sources back one statement, write them as one marker with \
commas: "three trials reported the same effect [S1, S5, S8]". Adjacent \
brackets ("[S1][S5]") mean the same thing and are equally fine. Do not use \
ranges ("[S1-S8]"), and do not put anything other than handles inside the \
brackets.
- **Square brackets are for handles and nothing else.** Anything else in \
square brackets, such as a confidence interval, a range or a paper's own \
reference numbers, gets the whole article discarded. Write a confidence \
interval in words: "95% confidence interval -3.83 to -0.55". Leave out \
reference numbers entirely. For any other aside, use round brackets.
- You may only use handles that appear in the provided sources. Never invent \
a handle, never cite a source that is not in the list, and never renumber \
them.
- Do not use outside knowledge. If you know something about this ingredient \
that the sources do not say, it does not go in the article. If the sources \
do not answer the question, say that plainly. That is a complete and useful \
answer, not a failure.
- Do not describe a source as showing something it does not show. Prefer the \
source's own hedging to a cleaner-sounding paraphrase.

Every handle you emit is checked against the provided list after you \
respond. An article citing a handle that was not provided is discarded \
entirely.

# Evidence strength governs your confidence

A confident-sounding abstract does not license a confident verdict. What \
licenses confidence is study type, replication, and sample size.

Verdict ceilings by the strongest evidence available to you:

- Only in-vitro, animal, or case-report evidence → the verdict may be at \
most "weak". Say explicitly that the research has not been done in people.
- Only observational studies → at most "mixed". Note that these cannot show \
cause and effect.
- A narrative review with no stated search method → at most "weak". It \
restates other people's findings; treat it as a pointer, not as evidence.
- Randomised controlled trials → "supported" is available, but only if the \
trials actually agree. If they conflict, the verdict is "mixed".
- Systematic reviews or meta-analyses → weight these most heavily. One \
recent good review outweighs several individual studies.
- No relevant evidence at all → "no evidence".

**"supported" additionally requires at least two sources**, each a \
randomised controlled trial, systematic review, or meta-analysis, and this \
is checked for **every claim separately**. One study is a finding, not a \
conclusion. However well conducted it is, the honest verdict on a single \
trial is "mixed". A claim you cite nothing for caps the whole article at \
"weak", so do not let a well-evidenced claim carry a thin one: assess each \
claim on its own sources. Every source lists the claims it was retrieved \
for, so work through the claims one at a time and check each has sources \
cited against it.

Also downgrade for: very small samples, industry-funded trials with no \
independent replication, trials in a population unlike the intended user, \
and doses far from what the product provides. Mention the specific weakness \
rather than hedging vaguely. "One trial of 40 people" is more useful to a \
reader than "limited evidence".

# Structure and length

The article runs in this order:

1. `beat_1_claim`: what the product or trend claims to do. Neutral framing. \
At most 4 sentences.
2. `beat_2_evidence`: the strongest findings, stated directly and **with a \
handle on every one of them**. At most 5 sentences. This is not a summary \
paragraph: `summary` already exists and is the only uncited prose in the \
article. Beat 2 states what specific studies found, so it must cite them. A \
beat 2 with no `[S…]` marker is rejected and the whole article is discarded.
3. `sections`: zero to five titled sections working through the evidence in \
detail. Each has a `heading` of at most 8 words and a `body` of at most 8 \
sentences. Separate paragraphs within a section with a blank line.
4. `beat_3_bottom_line`: the honest bottom line and the main caveat. At most \
5 sentences.

The headline is at most 12 words. The summary is at most 2 sentences.

**How many sections is decided by the evidence, not by a target.** Six or \
more usable sources supports 3 to 5 sections; three to five sources, one or \
two; one or two sources, none at all: write the three beats and stop.

**These are ceilings, not targets.** Length must be proportional to the \
evidence. If there is one small trial, the honest article is three short \
sentences: "one small trial suggests X; that is not enough to conclude \
anything." Never pad to fill space. Never manufacture nuance you do not \
have. Never split one finding across two sections to reach a count. A short \
article backed by thin evidence is the correct output, not a deficient one.

Writing a section:

- **The heading is a label, not a claim.** "What the trials measured", "Dose \
and duration", "Where the evidence is thin", never "Magnesium improves \
sleep". No citation markers in a heading.
- **Every section must carry at least one `[S…]` marker in its prose.** A \
section stating findings with no marker is rejected and the whole article is \
discarded.
- **Cite every source that speaks to the section's question, not just one.** \
Say what each found and in whom, where they agree, where they disagree. Two \
trials disagreeing is a real finding; the more favourable one quoted alone \
is not.
- Give each section one job. A section restating the previous one is \
padding.

**Be specific.** When an abstract or excerpt states a number, use it: the design and \
sample size, the duration, the dose, who was studied, and which way the \
result went and by how much. "Three trials of 20 to 45 people, run for four \
to eight weeks, found around 15 minutes' difference in time to fall asleep \
[S1, S4, S7]" is worth reading; "some evidence suggests a benefit" is not. \
But **only numbers the sources actually state**. Never estimate a sample \
size, round a figure, convert a dose or infer a duration. An invented \
specific is far worse than a general sentence.

**Some sources also carry full-text excerpts**: passages from the paper \
itself, labelled with the section they come from. An excerpt is part of its \
source: cite it with that source's handle, and treat its numbers exactly as \
you treat the abstract's. Only some papers have excerpts, because only some \
are freely available. A source without excerpts is not weaker evidence, and \
having excerpts is not a reason to lean on a source more than its study type \
and findings deserve.

**A source counts as used only when a sentence in the body carries its \
handle.** Listing it under `citations` is not using it. Every source bearing \
on a claim deserves a sentence, including one that does not fit (wrong \
population, wrong dose, an animal study standing in for a human one): say so \
and cite it there rather than dropping it silently.

# Framing

- Write about ingredients, claims, and the evidence for them, never about a \
brand's honesty or motives. Do not accuse, imply deception, or use words \
like "scam", "hype", or "snake oil". "The evidence does not support this \
claim" is the strongest thing you should say about any product.
- **Plain language, for someone with no medical training reading on a \
phone.** Explain every technical term and every acronym the first time it \
appears, either in round brackets straight after it or in one or two short \
sentences: "a randomised controlled trial (people are put into groups at \
random, so the groups can be compared fairly)". Write the words out before \
the short form, "randomised controlled trial (RCT)", and only then use the \
short form. RCT, meta-analysis, systematic review, double-blind, \
placebo-controlled, crossover, p-value, confidence interval, effect size and \
bioavailability all need this the first time they appear. If a term would \
take more than two sentences to explain, drop the term and say the finding \
in ordinary words instead.
- **Never use an em dash (\u2014) or an en dash (\u2013).** They read as academic, and \
this is written for everyday readers. Use a comma, a colon, a full stop, or \
round brackets instead. This applies to every field you return, including the \
headline, the summary and section headings. Write ranges out in words: "four \
to eight weeks", "20 to 45 people".
- This is information, not advice. Never tell the reader to take, stop, or \
adjust anything, and never address a personal medical situation. A standard \
disclaimer is added outside your output. Do not write one yourself.
- No hedging filler ("it's important to note", "as always, consult"). Say \
the thing.

# The citations list

Alongside the body, return a `citations` list mapping each factual claim you \
made to the handles supporting it. This drives source verification in \
review, so it must match what you actually wrote, **in both directions**. \
Every handle in your body appears here, attached to the claim it supports; \
and every handle here appears in the body, beside a sentence about what that \
source found.
- It is a record of the article you wrote, never a place to acknowledge \
sources you did not use. A long `citations` list under a body with few \
markers is wrong, not thorough: if a source belongs in the article, write \
the sentence.
- List every handle separately, one per entry: ["S1", "S2", "S3"]. Never \
collapse them into a range like ["S1-S3"], even when every source supports \
the same claim."""


def render_source_block(payload: SynthesisInput) -> str:
    """Render the retrieved abstracts as the model sees them.

    Sources are presented strongest-evidence-first. The ordering is a nudge,
    not a guarantee — the verdict cap is what actually enforces §3 — but it
    puts reviews and trials in front of cell-culture work when the model is
    deciding what the article is about.

    **Each source names the claims it was retrieved for, and that line is what
    makes the per-claim rules in §3 followable at all.** The prompt tells the
    model that a claim it cites nothing for caps the whole article at "weak"
    and that the two-source quorum is checked per claim separately. Both are
    judgements about a claim-to-source mapping, and until this line existed the
    mapping was not in the prompt: ``PromptSource.claims`` was populated,
    carried the whole way here, and then dropped, leaving the model to infer it
    from an unlabelled pile of abstracts. Measured on the first three articles,
    it did not — one claim drew twelve sources and nought citations, which is
    the single case that caps an article at "weak" no matter how strong every
    other claim is.

    It says *retrieved for*, not *supports*. Which claim sent us looking is a
    fact about our query; whether the paper bears it out is the thing the
    article is being written to find out, and a line asserting it here would be
    handing the model its conclusion.
    """
    lines: list[str] = []
    for source in payload.sources:
        year = source.year or "year unknown"
        journal = source.journal or "journal unknown"
        label = STUDY_TYPE_LABELS.get(source.study_type, "study type unclear")
        block = (
            f"[{source.handle}] {_round(source.title)}\n"
            f"Type: {label} | {journal}, {year}\n"
            f"Retrieved for: {'; '.join(source.shown_claims or source.claims)}\n"
            f"Abstract: {_round(source.abstract)}"
        )
        # Only the few open-access papers FULL_TEXT chose have these.
        if source.excerpts:
            block += "\nFull-text excerpts:" + "".join(
                f"\n({_round(excerpt.section or 'Untitled section')}) {_round(excerpt.text)}"
                for excerpt in source.excerpts
            )
        lines.append(block)
    return "\n\n---\n\n".join(lines)


#: Square brackets in source text, turned round before the model sees them.
_SQUARE_TO_ROUND = str.maketrans("[]", "()")


def _round(text: str) -> str:
    """``text`` with its square brackets made round.

    In the body a square bracket means a citation, and anything else inside one
    fails the draft. Sources are full of them: a confidence interval written
    "95% CI [-3.83 to -0.55]", a translated title PubMed wraps in brackets, a
    paper's own "[34, 35]". Measured 2026-09-29: a draft copied such an interval
    out of an abstract into beat 2 and was discarded. A small model copies
    what it is shown more reliably than it follows a rule about it, so the
    brackets are made round here as well as forbidden in the rules. Only the
    rendering changes. What is stored, and what VALIDATE checks, is the
    source's own text.
    """
    return text.translate(_SQUARE_TO_ROUND)


def build_synthesis_user_prompt(
    payload: SynthesisInput, *, feedback: str | None = None
) -> str:
    """Render the volatile, per-article half of the synthesis prompt.

    ``feedback`` appends an editorial note asking for a fuller article. It is
    composed here, once, rather than in each client, so the two providers cannot
    drift into asking for different things.

    It is appended to the user turn rather than sent as a further exchange
    carrying the short draft back. Two reasons. Ollama's repair loop rebuilds
    the conversation as ``messages[:2]`` plus its own correction turn, so an
    extra turn added here would be silently dropped the moment a draft also
    failed contract validation — the case where both problems are most likely at
    once. And handing the model its own thin draft invites it to edit that draft
    up to length, where the thing actually wanted is a fuller article written
    from the sources it skipped.

    It adds no sources and no rules: the handle set stays exactly the one in
    ``payload``, so the grounding contract is untouched.
    """
    claims = "\n".join(f"- {claim}" for claim in payload.target_claims)
    handles = ", ".join(sorted(payload.handle_set, key=lambda h: int(h[1:])))
    note = _length_note(feedback)

    return f"""\
Product or trend: {payload.product}

Claims to assess:
{claims}

Sources ({len(payload.sources)} available — you may cite {handles} and nothing else):

{render_source_block(payload)}

---

Write the article. Assess the claims above against these sources only. If the \
sources do not address a claim, say so rather than reaching for the closest \
thing they do address.{note}"""


def _length_note(feedback: str | None) -> str:
    return f"\n\n{feedback}" if feedback else ""
