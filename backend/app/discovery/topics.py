"""Turning a MeSH descriptor into a topic string a reviewer would have typed.

The output of this module becomes ``pipeline_runs.topic`` verbatim, which makes
it the narrowest and most consequential surface in the package: it is the only
thing discovery contributes to an article, and everything downstream —
``product``, ``ingredients``, the PubMed query, the verdict — is derived from it
by the extraction model.

That is also where the sharpest failure lives. ``TemplateQueryStrategy`` raises
``UnanchoredQuery`` when neither ``product`` nor ``ingredients`` names a
substance, and ``RetrieveStage`` converts it to a **non-retryable** stage
failure. So a topic phrased in a way that leads extraction to return
``ingredients: []`` and a vague ``product`` burns a model call and dies. The
shape below — bare substance first, outcome after "for" — is deliberately the
shape ``CreateRunRequest.topic`` documents and the extraction prompt was tuned
on ('ashwagandha for stress').
"""

from __future__ import annotations

from app.retrieval.query_builder import OUTCOME_HINTS

#: MeSH inverts headings for alphabetisation — "Fatty Acids, Omega-3", "Tea,
#: Herbal". Read aloud that is backwards, and it is going to a model that will
#: read it as a product name.
_INVERSION_SEPARATOR = ", "

#: Descriptor names carrying a parenthetical gloss ("Panax (plant)") or a
#: disambiguator. Stripped because the topic is prose, not an index entry.
_PARENTHETICAL = ("(", ")")


def humanise_descriptor(name: str) -> str:
    """A MeSH heading as someone would say it.

    ``"Fatty Acids, Omega-3"`` → ``"omega-3 fatty acids"``.

    Only the *first* comma is un-inverted. MeSH uses further commas for
    qualifiers that are not part of the name — "Tea, Herbal, Preparation" is not
    a thing anyone says — and reassembling all of them produces word salad.
    Lowercased because a capitalised term in the middle of a topic string reads
    as a brand, and the image prompt builder and the extraction model both take
    the topic as free text.
    """
    cleaned = name.strip()
    if _PARENTHETICAL[0] in cleaned:
        cleaned = cleaned.split(_PARENTHETICAL[0], 1)[0].strip()
    if _INVERSION_SEPARATOR in cleaned:
        head, tail = cleaned.split(_INVERSION_SEPARATOR, 1)
        tail = tail.split(_INVERSION_SEPARATOR, 1)[0]
        cleaned = f"{tail} {head}"
    return " ".join(cleaned.lower().split())


#: MeSH headings that name an outcome ``OUTCOME_HINTS`` calls something else.
#:
#: Substring matching gets most of the way — "Sleep Quality" contains "sleep",
#: "Muscle Strength" contains "muscle" — and then stops dead at the cases where
#: the indexed term and the spoken one share no letters. "Gut" and
#: "Gastrointestinal Microbiome" are the same subject and no amount of matching
#: in either direction connects them; that one matters because ``microbiome`` is
#: one of the eight seeds, so without this entry a whole net's worth of outcomes
#: reads as unrecognised.
#:
#: Keyed by a distinctive fragment of the MeSH heading, lowercased. Deliberately
#: short — this is a patch over a vocabulary gap, and the fix for a long list
#: here is a wider ``OUTCOME_HINTS``, which benefits retrieval too.
_HEADING_SYNONYMS: dict[str, str] = {
    "microbiome": "gut",
    "microbiota": "gut",
    "gastrointestinal": "gut",
    "endurance": "energy",
    "physical endurance": "energy",
    "hypertrophy": "muscle",
    "lean mass": "muscle",
    "body composition": "weight",
    "obesity": "weight",
    "adiposity": "weight",
    "depress": "mood",
    "affect": "mood",
    "insulin resistance": "blood sugar",
    "glycemic": "blood sugar",
    # "lipid" alone was too broad: it mapped "Lipid Peroxidation" — oxidative
    # damage — onto cholesterol, and produced "iron for lipid peroxidation" as a
    # proposed article. Narrowed to the headings that really are a lipid-profile
    # question.
    "lipids": "cholesterol",
    "lipid profile": "cholesterol",
    "lipoprotein": "cholesterol",
    "hyperlipidemia": "cholesterol",
    # Oxidative stress is not psychological stress, and bare substring matching
    # maps it there because OUTCOME_HINTS's "stress" key is a substring of it.
    # Sent to inflammation instead, which is the same article space and the same
    # readership; without this the suppression key conflates the two, so
    # dismissing "X for oxidative stress" would silence "X for stress".
    "oxidative": "inflammation",
    "antioxidant": "inflammation",
    "immunity": "immune",
    "osteoarthritis": "joint",
    "arthralgia": "joint",
    "alopecia": "hair",
    "insomnia": "sleep",
    "cardiovascular": "heart",
    "cardiac": "heart",
    "hypertension": "blood pressure",
    "osteoporosis": "bone",
    "bone density": "bone",
    "muscle soreness": "recovery",
    "aerobic": "endurance",
}


def outcome_keyword_for(name: str) -> str | None:
    """The ``OUTCOME_HINTS`` key this descriptor names, if any.

    Reuses the retrieval package's outcome vocabulary rather than declaring a
    second one. That is not only tidiness: a topic whose outcome word is already
    in ``OUTCOME_HINTS`` is one ``TemplateQueryStrategy`` can expand into real
    MeSH search terms later, so preferring those outcomes makes the eventual
    retrieval better as well as the sentence more readable.

    Direct substring first, then ``_HEADING_SYNONYMS`` for the headings whose
    indexed wording shares nothing with the spoken one. Longest match wins in
    both passes, so a compound key is not shadowed by a fragment of itself.

    The return value only decides *preference* between two co-occurring
    outcomes — the topic string itself is always built from the descriptor's own
    humanised name. So a miss here costs a slightly worse-worded topic, never a
    wrong one.
    """
    haystack = name.lower()
    # Synonyms first. They are curated for exactly the headings where plain
    # substring matching lands on the wrong key — "Oxidative Stress" contains
    # "stress", and OUTCOME_HINTS's "stress" means cortisol and anxiety. A
    # direct-match-first order would make those entries unreachable.
    synonyms = [
        keyword for fragment, keyword in _HEADING_SYNONYMS.items() if fragment in haystack
    ]
    if synonyms:
        return max(synonyms, key=len)
    direct = [key for key in OUTCOME_HINTS if key in haystack]
    if direct:
        return max(direct, key=len)
    return None


def compose_topic(substance_name: str, outcome_name: str | None) -> str:
    """The topic string a promoted candidate enqueues.

    ``"tongkat ali for testosterone"``. The substance is bare and leads, because
    that is the token extraction has to recover as ``product``/``ingredients``
    and everything downstream is anchored on it.

    A missing outcome yields the substance alone rather than an invented one. A
    bare topic still extracts and still retrieves — the query is anchored on the
    subject with no outcome group, which ``_compose`` explicitly supports — while
    a guessed outcome would put a claim in the topic that nothing in the
    literature suggested.
    """
    substance = humanise_descriptor(substance_name)
    if not outcome_name:
        return substance
    outcome = humanise_descriptor(outcome_name)
    if not outcome or outcome == substance:
        return substance
    return f"{substance} for {outcome}"


def topic_question(substance_name: str, outcome_name: str | None) -> str:
    """The question a topic's article would answer, for the desk. No model call.

    "affect" on purpose: the scan knows only that papers mention both, not which
    way they point. ``compose_topic``'s string is still what a promotion generates
    from. A substance with no outcome gets no invented one.
    """
    substance = humanise_descriptor(substance_name)
    last = substance.split()[-1] if substance else ""
    plural = last.endswith("s") and not last.endswith(("ss", "us", "is"))
    verb = "Do" if plural else "Does"
    outcome = humanise_descriptor(outcome_name) if outcome_name else ""
    if not outcome or outcome == substance:
        return f"{verb} {substance} have any proven health benefits?"
    return f"{verb} {substance} affect {outcome}?"


def rank_outcomes(
    counts: dict[str, int], names: dict[str, str]
) -> list[tuple[str, str]]:
    """Co-occurring outcome descriptors, best first.

    Ordered by whether the descriptor maps into ``OUTCOME_HINTS`` and only then
    by how often it co-occurred. A recognised outcome on four papers makes a
    better topic than an unrecognised one on nine: the recognised word is what a
    reader searches for and what the query builder can expand, while the
    unrecognised one is usually a MeSH abstraction nobody says out loud
    ("Physiological Phenomena").

    Ties break on the descriptor UI so the same window always produces the same
    topic — a candidate whose text changed between a dry run and the real one
    would be unreviewable.
    """
    ranked = sorted(
        counts.items(),
        key=lambda item: (
            outcome_keyword_for(names.get(item[0], "")) is not None,
            item[1],
            item[0],
        ),
        reverse=True,
    )
    return [(ui, names.get(ui, "")) for ui, _ in ranked]


def is_usable_angle(substance_name: str, outcome_name: str | None) -> bool:
    """Whether this outcome is worth cutting a separate proposal for.

    Stricter than "is it an outcome". Every descriptor that is not a substance
    falls through to outcome, which is fine for *counting* and much too
    permissive for *naming an article*. Three real proposals from the live scan
    on 2026-09-06 show the three ways it goes wrong:

    * ``vitamin d for cross-sectional studies`` — a study design. Now stoplisted,
      but the next one will not be.
    * ``vitamin d for vitamin d deficiency`` — tautological: the outcome restates
      the substance, so the topic asks nothing.
    * ``catechin for tea`` — a food. Not a design, not a tautology, and still not
      a question anyone has.

    Only the first is fixable by a list. So an angle must name an outcome in the
    vocabulary this product already speaks — ``OUTCOME_HINTS`` — which is the
    same set ``TemplateQueryStrategy`` can expand into real MeSH search terms if
    the candidate is promoted. Anything else is not refused: the substance is
    still proposed under a bare topic, exactly as it was before angles existed.
    The rule costs recall on unusual-but-real outcomes and buys never publishing
    a topic phrased in index vocabulary.
    """
    if not outcome_name:
        return False
    if outcome_keyword_for(outcome_name) is None:
        return False
    # Tautology guard. "Vitamin D" against "Vitamin D Deficiency" shares a
    # keyword the hint map cannot see is redundant, and the resulting topic asks
    # nothing. Compared on the humanised forms, so MeSH's inverted headings do
    # not slip past on punctuation.
    substance = humanise_descriptor(substance_name)
    outcome = humanise_descriptor(outcome_name)
    return substance not in outcome and outcome not in substance
