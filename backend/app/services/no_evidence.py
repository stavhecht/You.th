"""The article a topic gets when the literature has nothing to say.

Built in code, never by a model. Every other article in this system earns its
sentences from retrieved abstracts; this one has none, so there is nothing for
a generative call to be grounded in. Handing a model a product name and zero
sources is precisely the ungrounded generation the rest of the pipeline exists
to prevent — and it would arrive carrying no citations, so ``validate_draft``
would have nothing to check and invariant #2 would pass by vacuum rather than
by verification.

So the prose is a template. It says three things and cannot be induced to say a
fourth: what the product claims, that a search found nothing meeting our
evidence criteria, and that absence of research is not a finding against the
product. That last sentence is the one worth being careful about — "no evidence
for" is not "evidence against", and a reader who takes the first for the second
has been misled by an article that was technically accurate.

Reached from two causes that are identical to a reader but not to you (see
``pipeline/steps/synthesize.py``):

* retrieval returned no candidates at all — a genuinely unstudied trend;
* candidates were retrieved but none survived the rank filters (minimum year,
  minimum abstract length, grade floor).

The stage metrics record which, because "science has not studied this" and "our
filters are too tight" need the same sentence to a reader and very different
responses from us.
"""

from __future__ import annotations

from collections.abc import Callable

from app.domain.contracts import ArticleBody, SynthesisOutput, count_sentences
from app.domain.enums import Verdict

#: Mirrors the validators in ``SynthesisOutput`` / ``ArticleBody``. Checked here
#: rather than left to raise, because a long product name is not a reason to
#: fail a run that is otherwise working exactly as intended — it degrades to a
#: shorter phrasing instead.
HEADLINE_MAX_WORDS = 12
#: Beat 1's own ceiling, which is the only one this module interpolates into.
#: Named for the beat rather than for beats in general because the three no
#: longer share a number — beats 2 and 3 allow 5. See ``ArticleBody``.
MAX_CLAIM_BEAT_SENTENCES = 4

#: Fixed text. No interpolation, so these need no length guard and cannot drift
#: with the input.
#:
#: All three refer to "the claims" and never to the product as "it", for the
#: reason given in ``_claim_beat``: the topic is as likely to be a plural trend
#: as a singular product, and these strings are shared by both.
SUMMARY = (
    "No published studies matching these claims met our evidence criteria. "
    "That is not a finding against the claims, and our search may have missed studies "
    "that describe them in other words."
)

BEAT_2_EVIDENCE = (
    "A search of the published literature for each of these claims returned no "
    "studies that met our evidence criteria."
)

#: The load-bearing sentence in the whole module. "No evidence for" and
#: "evidence against" are different statements, and a reader who takes the
#: first for the second has been misled by an article that was accurate.
#: It also never says "not studied": wt_025's search for one seven-word phrase found
#: nothing and said so, while PubMed held 147 papers on lemon water (2026-11-07).
BEAT_3_BOTTOM_LINE = (
    "No evidence found is not the same as evidence of no effect: the question may "
    "be unstudied, or studied under words our search did not use. Treat confident "
    "marketing claims with corresponding caution."
)


def no_evidence_draft(product: str, target_claims: list[str]) -> SynthesisOutput:
    """The complete draft for a topic with no usable sources.

    Returns a ``SynthesisOutput`` shaped exactly like a generated one, so every
    downstream stage — validation, TipTap assembly, persistence, card
    derivation — runs unchanged. Validation passes it on its own merits rather
    than by exemption: the emitted handle set is empty (check 1 and check 2 have
    nothing to reject), ``NO_EVIDENCE`` is exempt from the cited-beat rule, and
    ``best_grade([])`` is ``UNKNOWN``, whose ceiling of ``weak`` sits above
    ``no_evidence``. The article reaches the review queue as ``0/0 citations
    resolve``, and a human still approves it.
    """
    return SynthesisOutput(
        headline=_headline(product),
        verdict=Verdict.NO_EVIDENCE,
        verdict_qualifier=None,
        summary=SUMMARY,
        body=ArticleBody(
            beat_1_claim=_claim_beat(product, target_claims),
            beat_2_evidence=BEAT_2_EVIDENCE,
            beat_3_bottom_line=BEAT_3_BOTTOM_LINE,
        ),
        citations=[],
    )


def _headline(product: str) -> str:
    return _first_that_fits(
        [f"No published evidence for {product}", "No published evidence found"],
        fits=lambda text: len(text.split()) <= HEADLINE_MAX_WORDS,
    )


def _claim_beat(product: str, target_claims: list[str]) -> str:
    """Beat 1 — restate what the product claims, in its own terms.

    "Marketing for X" is the grammatical subject, and the product is never
    referred to as "it". Both choices are deliberate: half the topics this
    system handles are trends rather than products and arrive plural ("cold
    plunges", "greens powders"), while extraction emits claims as
    third-person-*singular* verb phrases ("reduces stress"). Any phrasing that
    makes the product the subject, or pronouns it, therefore reads as
    "Cold plunges is marketed on the claim that it reduces soreness" — the
    first sentence a reader sees, ungrammatical, in a product whose entire
    proposition is that it is more careful than the marketing it checks.
    Anchoring the verb to "Marketing" makes the number of the product
    irrelevant.
    """
    claims = [cleaned for claim in target_claims if (cleaned := _clean_claim(claim))]

    candidates = []
    if claims:
        preamble = "one claim" if len(claims) == 1 else "these claims"
        candidates.append(f"Marketing for {product} makes {preamble}: {_join(claims)}.")
    candidates.append(f"Marketing for {product} makes health claims.")
    candidates.append("This product is marketed with health claims.")

    return _first_that_fits(
        candidates, fits=lambda text: count_sentences(text) <= MAX_CLAIM_BEAT_SENTENCES
    )


def _clean_claim(claim: str) -> str:
    """Strip the trailing punctuation that would add a sentence to the beat."""
    return claim.strip().rstrip(".!?;,").strip()


def _join(items: list[str]) -> str:
    """Oxford-comma join: 'a', 'a and b', 'a, b, and c'."""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return f"{', '.join(items[:-1])}, and {items[-1]}"


def _first_that_fits(candidates: list[str], *, fits: Callable[[str], bool]) -> str:
    """The first candidate within bounds, else the last (shortest) one.

    Ordered most-specific first. The final candidate interpolates nothing the
    caller supplied, so the fallback chain always terminates in something that
    fits regardless of what extraction produced.
    """
    for text in candidates:
        if fits(text):
            return text
    return candidates[-1]
