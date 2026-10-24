"""Content-shaping tests: field bounds, TipTap conversion, card derivation.

The card tests are the ones that matter most here. "The feed card is derived
from the article, not generated separately" is a structural guarantee, and
these are what hold it.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.domain.contracts import (
    ArticleBody,
    ArticleSection,
    CitationGroup,
    ExtractionOutput,
    SynthesisOutput,
    count_sentences,
    extract_handles,
)
from app.domain.enums import Verdict
from app.evidence.validation import check_body_parses
from app.services.card import CARD_EXCERPT_MAX_CHARS, derive_card
from app.services.tiptap import (
    MalformedBodyError,
    beat_text,
    body_text_to_doc,
    cited_handles_in_doc,
    doc_to_plain_text,
)

# ---------------------------------------------------------------------------
# Field bounds (DESIGN.md §6). Ceilings, enforced per field rather than as a
# global word count, so thin evidence yields a short honest article.
# ---------------------------------------------------------------------------


def test_headline_over_twelve_words_is_rejected() -> None:
    with pytest.raises(ValidationError, match="12 words"):
        SynthesisOutput(
            headline=" ".join(["word"] * 13),
            verdict=Verdict.WEAK,
            summary="Short.",
            body=ArticleBody(
                beat_1_claim="Claims things.",
                beat_2_evidence="Evidence [S1].",
                beat_3_bottom_line="Bottom line.",
            ),
        )


def test_claim_beat_over_four_sentences_is_rejected() -> None:
    with pytest.raises(ValidationError, match="4 sentences"):
        ArticleBody(
            beat_1_claim="One. Two. Three. Four. Five.",
            beat_2_evidence="Evidence [S1].",
            beat_3_bottom_line="Bottom line.",
        )


def test_evidence_beat_over_five_sentences_is_rejected() -> None:
    with pytest.raises(ValidationError, match="5 sentences"):
        ArticleBody(
            beat_1_claim="Claims.",
            beat_2_evidence="One. Two. Three. Four. Five. Six [S1].",
            beat_3_bottom_line="Bottom line.",
        )


def test_a_section_over_eight_sentences_is_rejected() -> None:
    with pytest.raises(ValidationError, match="8 sentences"):
        ArticleSection(heading="What the trials measured", body=". ".join(["One"] * 9) + ".")


def test_a_section_heading_over_eight_words_is_rejected() -> None:
    with pytest.raises(ValidationError, match="8 words"):
        ArticleSection(heading=" ".join(["word"] * 9), body="Prose [S1].")


def test_more_than_five_sections_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ArticleBody(
            beat_1_claim="Claims.",
            beat_2_evidence="Evidence [S1].",
            sections=[ArticleSection(heading=f"H{n}", body=f"Body [S{n}].") for n in range(6)],
            beat_3_bottom_line="Bottom line.",
        )


def test_sections_have_no_minimum() -> None:
    """The ceiling rose; the floor stayed at zero.

    A `min_length` here would be a padding instruction — an article resting on
    one trial would be obliged to invent things to say about it. DESIGN.md §6.
    """
    body = ArticleBody(
        beat_1_claim="Claims.",
        beat_2_evidence="One small trial found a modest effect [S1].",
        beat_3_bottom_line="Not enough to conclude anything.",
    )
    assert body.sections == []


def test_as_text_includes_sections() -> None:
    """Citations inside a section must reach `cited_handles`.

    Otherwise `was_cited` is false for sources the article visibly cites, and
    the strongest evidence in the article is invisible to validation.
    """
    body = ArticleBody(
        beat_1_claim="Claims.",
        beat_2_evidence="Evidence [S1].",
        sections=[
            ArticleSection(heading="Dose and duration", body="Eight weeks at 300mg [S4]."),
            ArticleSection(heading="Where it is thin", body="No trial ran past a year [S9]."),
        ],
        beat_3_bottom_line="Bottom line.",
    )
    assert body.cited_handles() == {"S1", "S4", "S9"}
    assert "Dose and duration" in body.as_text()


def test_summary_over_two_sentences_is_rejected() -> None:
    with pytest.raises(ValidationError, match="2 sentences"):
        SynthesisOutput(
            headline="Short headline",
            verdict=Verdict.WEAK,
            summary="One. Two. Three.",
            body=ArticleBody(
                beat_1_claim="Claims.",
                beat_2_evidence="Evidence [S1].",
                beat_3_bottom_line="Bottom.",
            ),
        )


def test_a_one_sentence_article_is_valid() -> None:
    """Length is a ceiling, not a floor.

    Thin evidence must be allowed to produce a short article; nothing in the
    system may require padding to reach a length.
    """
    output = SynthesisOutput(
        headline="Not enough evidence yet",
        verdict=Verdict.WEAK,
        summary="One small trial is not enough to conclude anything.",
        body=ArticleBody(
            beat_1_claim="It claims to improve sleep.",
            beat_2_evidence="One small trial of 40 people found a modest effect [S1].",
            beat_3_bottom_line="That is not enough to conclude anything.",
        ),
    )
    assert output.verdict is Verdict.WEAK


def test_verdict_qualifier_must_be_one_short_clause() -> None:
    with pytest.raises(ValidationError, match="single short clause"):
        SynthesisOutput(
            headline="Headline",
            verdict=Verdict.MIXED,
            verdict_qualifier=" ".join(["word"] * 20),
            summary="Short.",
            body=ArticleBody(
                beat_1_claim="Claims.",
                beat_2_evidence="Evidence [S1].",
                beat_3_bottom_line="Bottom.",
            ),
        )


def test_sentence_counting_tolerates_trailing_whitespace() -> None:
    assert count_sentences("One. Two.  ") == 2
    assert count_sentences("No terminator") == 1


def test_handle_extraction_finds_multi_digit_handles() -> None:
    assert extract_handles("a [S1] b [S12] c") == {"S1", "S12"}


# ---------------------------------------------------------------------------
# Citation handle shape.
#
# A run was lost to `source_ids: ["S1-S8"]` — the shorthand a model reaches for
# when every source backs the same claim. Two independent defences: the schema
# pattern stops it being generated, the before-validator repairs it if some
# other provider emits it anyway.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("shorthand", "expected"),
    [
        ("S1-S8", [f"S{n}" for n in range(1, 9)]),
        ("S1-3", ["S1", "S2", "S3"]),
        ("S1\u2013S3", ["S1", "S2", "S3"]),  # en dash
        ("S2 - S4", ["S2", "S3", "S4"]),
        ("S5-S5", ["S5"]),
    ],
)
def test_handle_ranges_expand(shorthand: str, expected: list[str]) -> None:
    assert CitationGroup(claim="c", source_ids=[shorthand]).source_ids == expected


def test_comma_packed_handles_split() -> None:
    group = CitationGroup(claim="c", source_ids=["S1, S2", "S4"])
    assert group.source_ids == ["S1", "S2", "S4"]


def test_overlapping_shorthand_dedupes_in_order() -> None:
    group = CitationGroup(claim="c", source_ids=["S1-S3", "S2"])
    assert group.source_ids == ["S1", "S2", "S3"]


def test_absurd_range_is_rejected_not_expanded() -> None:
    # Inventing 900 handles we never retrieved is worse than failing loudly.
    with pytest.raises(ValidationError):
        CitationGroup(claim="c", source_ids=["S1-S900"])


@pytest.mark.parametrize("handle", ["SX", "1", "S", "S1x", "", "source 1"])
def test_genuinely_malformed_handles_still_rejected(handle: str) -> None:
    with pytest.raises(ValidationError):
        CitationGroup(claim="c", source_ids=[handle])


def test_handle_pattern_reaches_the_structured_output_schema() -> None:
    """The constraint is only worth anything if the grammar can see it."""
    schema = SynthesisOutput.model_json_schema()
    items = schema["$defs"]["CitationGroup"]["properties"]["source_ids"]["items"]
    assert items["pattern"] == "^S[0-9]+$"


def test_schema_patterns_avoid_escapes_ollama_cannot_compile() -> None:
    """Guard the `[0-9]`-not-`\\d` decision in contracts.py.

    Ollama compiles `pattern` into GBNF and its converter rejects the `\\d`
    class escapes outright — the request fails at 400 "failed to parse grammar",
    which takes down *every* call using the schema, not just a malformed one.
    Tidying `[0-9]` back to `\\d` therefore looks harmless and is not.
    """
    unsupported = ("\\d", "\\w", "\\s", "\\D", "\\W", "\\S")

    def patterns(node: object) -> list[str]:
        if isinstance(node, dict):
            found = [node["pattern"]] if isinstance(node.get("pattern"), str) else []
            return found + [p for v in node.values() for p in patterns(v)]
        if isinstance(node, list):
            return [p for item in node for p in patterns(item)]
        return []

    for model in (SynthesisOutput, ExtractionOutput):
        for pattern in patterns(model.model_json_schema()):
            assert not any(esc in pattern for esc in unsupported), (
                f"{model.__name__}: pattern {pattern!r} uses an escape Ollama "
                f"cannot compile; use an explicit class like [0-9]"
            )


# ---------------------------------------------------------------------------
# TipTap conversion.
# ---------------------------------------------------------------------------


def _body(beat_2: str = "Cortisol fell [S1].") -> ArticleBody:
    return ArticleBody(
        beat_1_claim="It claims to reduce stress.",
        beat_2_evidence=beat_2,
        beat_3_bottom_line="Early evidence only.",
    )


def test_citations_become_first_class_nodes() -> None:
    doc = body_text_to_doc(_body())
    paragraph = doc["content"][1]
    types = [child["type"] for child in paragraph["content"]]
    assert "citation" in types
    citation = next(c for c in paragraph["content"] if c["type"] == "citation")
    assert citation["attrs"]["sourceIds"] == ["S1"]


def test_adjacent_markers_collapse_into_one_node() -> None:
    """`[S1][S3]` is one chip, not two.

    A row of separate chips reads as several findings when it is one.
    """
    doc = body_text_to_doc(_body("Cortisol fell [S1][S3] in both trials."))
    paragraph = doc["content"][1]
    citations = [c for c in paragraph["content"] if c["type"] == "citation"]
    assert len(citations) == 1
    assert citations[0]["attrs"]["sourceIds"] == ["S1", "S3"]


def test_beats_are_tagged_by_number_not_position() -> None:
    doc = body_text_to_doc(_body())
    assert [node["attrs"]["beat"] for node in doc["content"]] == [1, 2, 3]


def _source_ids(beat_2: str) -> list[list[str]]:
    """The sourceIds of every citation node in beat 2, in order."""
    paragraph = body_text_to_doc(_body(beat_2))["content"][1]
    return [c["attrs"]["sourceIds"] for c in paragraph["content"] if c["type"] == "citation"]


@pytest.mark.parametrize(
    "marker",
    ["[S1, S5, S8]", "[S1,S5,S8]", "[S1 , S5 , S8]", "[S1][S5][S8]", "[S1, S5][S8]"],
)
def test_every_multi_source_spelling_yields_one_node(marker: str) -> None:
    """The regression that cost a draft.

    The prompt only ever showed a single handle, so llama3.1:8b cited three
    sources as `[S1, S5, S8]`. The parser accepted only adjacent brackets, the
    body failed to render, and the article was written `validation_failed` —
    invisible to the reviewer, because that status never enters the queue.
    """
    assert _source_ids(f"Cortisol fell {marker} in three trials.") == [["S1", "S5", "S8"]]


@pytest.mark.parametrize(
    "marker", ["[S1, S5, S8]", "[S1,S5,S8]", "[S1][S5][S8]", "[S1, S5][S8]"]
)
def test_handle_extraction_agrees_with_the_parser(marker: str) -> None:
    """The two must never disagree about what a marker is.

    `extract_handles` feeds `ArticleBody.cited_handles`, which sets `was_cited` and
    backs `check_beats_are_cited`. A form the parser renders but the extractor
    cannot read means a source the article visibly cites is stored as uncited,
    and a fully cited beat is reported as uncited.
    """
    assert extract_handles(marker) == {"S1", "S5", "S8"}


def test_a_handle_repeated_across_spellings_dedupes_in_order() -> None:
    assert _source_ids("Cortisol fell [S3, S1][S3].") == [["S3", "S1"]]


@pytest.mark.parametrize(
    "marker",
    ["[S1, blah]", "[S1", "[S1, S5", "[sic]", "[S]", "[source 1]", "hanging ]"],
)
def test_a_bracket_that_is_not_a_marker_still_fails(marker: str) -> None:
    """Widening the accepted forms must not widen them to anything bracketed.

    `[S1, S5` is the one that used to slip: it matched neither the old
    malformed pattern nor the marker pattern, so it fell through as literal
    text and printed a broken marker into the finished article.
    """
    with pytest.raises(MalformedBodyError):
        body_text_to_doc(_body(f"Cortisol fell {marker} in both trials."))


def test_a_bare_handle_in_prose_is_not_a_citation() -> None:
    """The handle pattern is unanchored, so the run has to be matched whole —
    otherwise "the S1 group" becomes a citation node."""
    assert _source_ids("Cortisol fell in the S1 group.") == []


def test_a_comma_marker_round_trips_through_the_canonical_form() -> None:
    """`doc_to_plain_text` regenerates adjacent brackets. That is a different
    spelling of the same input, so it has to re-parse to the same document —
    this is the path human edits are re-validated on."""
    doc = body_text_to_doc(_body("Cortisol fell [S1, S5]."))
    restored = doc_to_plain_text(doc, keep_citations=True)

    assert "[S1][S5]" in restored
    assert body_text_to_doc(_body(restored.split("\n\n")[1])) == doc


def test_malformed_marker_raises() -> None:
    """A body we cannot render is a draft problem, surfaced as a failure code."""
    with pytest.raises(MalformedBodyError):
        body_text_to_doc(_body("Cortisol fell [S1 and rose [source 2]."))


def test_an_unparseable_body_fails_validation_and_keeps_what_the_model_wrote() -> None:
    """Measured 2026-09-29: a draft copied "[-3.83 to -0.55]" from an abstract.
    The failure was added in PERSIST, after VALIDATE's metrics said "passed",
    and the article was stored with an empty body and no trace of the text."""
    beat_2 = "Anxiety scores fell (MD -2.19, 95% CI [-3.83 to -0.55]) [S7]."
    output = SynthesisOutput(
        headline="Ashwagandha and stress: what the evidence shows",
        verdict=Verdict.MIXED,
        summary="A short summary sentence.",
        body=_body(beat_2),
    )

    (failure,) = check_body_parses(output)

    assert failure.code == "malformed_body"
    assert failure.detail == {"where": "beat 2", "text": beat_2}


def test_round_trip_preserves_citations() -> None:
    body = _body("Cortisol fell [S1] and sleep improved [S3].")
    doc = body_text_to_doc(body)
    restored = doc_to_plain_text(doc, keep_citations=True)
    assert "[S1]" in restored
    assert "[S3]" in restored


def test_stripping_citations_leaves_clean_prose() -> None:
    doc = body_text_to_doc(_body("Cortisol fell [S1]."))
    assert "[S1]" not in doc_to_plain_text(doc, keep_citations=False)


def test_cited_handles_in_doc_finds_every_handle() -> None:
    """Used at approve time to re-validate human-edited content."""
    doc = body_text_to_doc(_body("A [S1] and B [S2][S3]."))
    assert cited_handles_in_doc(doc) == {"S1", "S2", "S3"}


def test_beat_text_addresses_by_tag_not_index() -> None:
    doc = body_text_to_doc(_body())
    # Simulate a reviewer inserting a paragraph ahead of the beats.
    doc["content"].insert(0, {"type": "paragraph", "attrs": {}, "content": []})
    assert beat_text(doc, beat=1).startswith("It claims")


# --- sections ---------------------------------------------------------------


def _sectioned() -> ArticleBody:
    return ArticleBody(
        beat_1_claim="It claims to reduce stress.",
        beat_2_evidence="Cortisol fell [S1].",
        sections=[
            ArticleSection(
                heading="What the trials measured",
                body="Two trials of 40 adults [S1].\n\nBoth ran eight weeks [S2].",
            ),
            ArticleSection(heading="Where it is thin", body="No trial ran past a year [S3]."),
        ],
        beat_3_bottom_line="Early evidence only.",
    )


def test_sections_render_between_beats_two_and_three() -> None:
    doc = body_text_to_doc(_sectioned())
    shape = [
        (node["type"], node.get("attrs", {}).get("beat"), node.get("attrs", {}).get("level"))
        for node in doc["content"]
    ]
    assert shape == [
        ("paragraph", 1, None),
        ("paragraph", 2, None),
        ("heading", None, 2),
        ("paragraph", None, None),  # blank line split this section in two
        ("paragraph", None, None),
        ("heading", None, 2),
        ("paragraph", None, None),
        ("paragraph", 3, None),
    ]


def test_a_section_paragraph_is_not_a_fourth_beat() -> None:
    """Section paragraphs carry no `beat`, exactly as a reviewer's do.

    `beat_text` must keep finding the three beats, or the card excerpt starts
    coming from whichever paragraph happens to sit first.
    """
    doc = body_text_to_doc(_sectioned())
    tagged = [n for n in doc["content"] if n.get("attrs", {}).get("beat") is not None]
    assert len(tagged) == 3
    assert beat_text(doc, beat=1).startswith("It claims")
    assert beat_text(doc, beat=3).startswith("Early evidence")


def test_section_citations_survive_the_conversion() -> None:
    doc = body_text_to_doc(_sectioned())
    assert cited_handles_in_doc(doc) == {"S1", "S2", "S3"}


def test_a_section_heading_becomes_a_level_two_heading() -> None:
    doc = body_text_to_doc(_sectioned())
    heading = doc["content"][2]
    assert heading["attrs"]["level"] == 2
    assert heading["content"][0]["text"] == "What the trials measured"


def test_a_malformed_marker_in_a_section_is_caught() -> None:
    body = ArticleBody(
        beat_1_claim="Claims.",
        beat_2_evidence="Evidence [S1].",
        sections=[ArticleSection(heading="Detail", body="Unterminated [S1, S5 here.")],
        beat_3_bottom_line="Bottom.",
    )
    with pytest.raises(MalformedBodyError, match="section 1"):
        body_text_to_doc(body)


def test_the_card_excerpt_is_unchanged_by_sections() -> None:
    """Sections sit after beat 1, so the tile must not notice them."""
    doc = body_text_to_doc(_sectioned())
    card = derive_card("Ashwagandha and stress", doc, Verdict.MIXED)
    assert card.excerpt == "It claims to reduce stress."


# ---------------------------------------------------------------------------
# Card derivation. One AI output, two renderings.
# ---------------------------------------------------------------------------


def test_card_excerpt_is_first_sentence_of_beat_one_without_markers() -> None:
    doc = body_text_to_doc(
        ArticleBody(
            beat_1_claim="It claims to reduce stress [S1]. It also claims better sleep.",
            beat_2_evidence="A trial found lower cortisol [S1].",
            beat_3_bottom_line="Early days.",
        )
    )
    card = derive_card("Ashwagandha and stress", doc, Verdict.MIXED)

    assert card.excerpt == "It claims to reduce stress."
    assert "[S1]" not in card.excerpt


def test_card_verdict_always_matches_the_article_verdict() -> None:
    """The property a separate card-generation call would not give us."""
    doc = body_text_to_doc(_body())
    for verdict in Verdict:
        assert derive_card("Headline", doc, verdict).verdict is verdict


def test_long_excerpt_truncates_on_a_word_boundary() -> None:
    """Mid-word truncation reads as a rendering bug, which costs credibility."""
    long_claim = "It claims " + "to do many different wellness things " * 10
    doc = body_text_to_doc(
        ArticleBody(
            beat_1_claim=long_claim.strip() + ".",
            beat_2_evidence="Evidence [S1].",
            beat_3_bottom_line="Bottom.",
        )
    )
    card = derive_card("Headline", doc, Verdict.WEAK)

    assert len(card.excerpt) <= CARD_EXCERPT_MAX_CHARS
    assert card.excerpt.endswith("…")
    assert not card.excerpt[:-1].endswith(" ")


def test_card_survives_an_empty_body() -> None:
    """A malformed draft still persists (as validation_failed); deriving a card
    from its empty document must not raise."""
    card = derive_card("Headline", {"type": "doc", "content": []}, Verdict.NO_EVIDENCE)
    assert card.excerpt == ""


def test_card_image_is_the_articles_own_first_picture() -> None:
    """The tile shows what the article shows, and nothing chosen separately.

    A card image field a reviewer could set independently is the same failure
    the derived headline and excerpt exist to prevent, in picture form: a tile
    promising something the article does not contain.
    """
    doc = body_text_to_doc(_body())
    doc["content"].insert(
        1,
        {
            "type": "image",
            "attrs": {"src": "/api/media/ab/first.png", "alt": "A lemon"},
        },
    )
    doc["content"].append(
        {"type": "image", "attrs": {"src": "/api/media/cd/second.png"}}
    )

    card = derive_card("Headline", doc, Verdict.MIXED)

    assert card.image == "/api/media/ab/first.png"
    assert card.image_alt == "A lemon"


def test_a_youtube_block_is_not_a_card_image() -> None:
    """Only `image` nodes count.

    A video's thumbnail lives on a third-party host, so deriving one here would
    put a YouTube request on every render of the feed — which is exactly the
    tracking the article page's click-to-load facade exists to avoid.
    """
    doc = body_text_to_doc(_body())
    doc["content"].insert(1, {"type": "youtube", "attrs": {"videoId": "dQw4w9WgXcQ"}})

    assert derive_card("Headline", doc, Verdict.MIXED).image is None


def test_no_image_is_a_normal_card_not_a_failure() -> None:
    """Most articles have no picture; the feed draws a typographic tile."""
    card = derive_card("Headline", body_text_to_doc(_body()), Verdict.WEAK)

    assert card.image is None
    assert card.image_alt is None


# --- the generated cover, and the pairing that keeps it honest --------------
#
# The rule is that a feed tile cannot show a picture the article does not
# contain. The pipeline's portrait cover is, read literally, exactly such a
# picture — it is never in the document. What makes it legitimate is that both
# frames were drawn in one step from one prompt for this article, and that the
# document's own first image is still the landscape one of the pair. These
# tests are where that "still" is enforced.

_GEN_LEAD = "/api/media/ab/" + "1" * 62 + ".webp"
_GEN_COVER = "/api/media/cd/" + "2" * 62 + ".webp"


def _generated(lead: str = _GEN_LEAD, cover: str = _GEN_COVER) -> dict:
    return {
        "prompt": "a still life",
        "negative_prompt": "person",
        "model": "huggingface/black-forest-labs/FLUX.1-schnell",
        "lead": {"src": lead, "alt": "A still life", "width": 1216, "height": 832, "seed": 1},
        "cover": {"src": cover, "alt": "A still life", "width": 864, "height": 1152, "seed": 1},
    }


def _doc_with_lead(src: str = _GEN_LEAD) -> dict:
    doc = body_text_to_doc(_body())
    doc["content"].insert(0, {"type": "image", "attrs": {"src": src, "alt": "A still life"}})
    return doc


def test_the_tile_shows_the_generated_cover_while_the_lead_is_untouched() -> None:
    """The normal path: portrait on the tile, landscape in the article."""
    card = derive_card(
        "Headline", _doc_with_lead(), Verdict.MIXED, generated=_generated()
    )

    assert card.image == _GEN_COVER
    assert card.image_is_generated_cover is True
    # Alt comes from the document's node in both branches: the two frames show
    # the same still life, and a reviewer editing the alt described the picture.
    assert card.image_alt == "A still life"


def test_replacing_the_lead_image_drops_the_generated_cover() -> None:
    """The reviewer swapped the picture; the pair is broken by the only person who could."""
    reviewer_upload = "/api/media/ef/" + "3" * 62 + ".png"
    card = derive_card(
        "Headline",
        _doc_with_lead(reviewer_upload),
        Verdict.MIXED,
        generated=_generated(),
    )

    assert card.image == reviewer_upload
    assert card.image_is_generated_cover is False


def test_deleting_the_lead_image_drops_the_generated_cover() -> None:
    """Failing toward "no cover" rather than "wrong cover" is the whole design."""
    card = derive_card(
        "Headline", body_text_to_doc(_body()), Verdict.MIXED, generated=_generated()
    )

    assert card.image is None
    assert card.image_is_generated_cover is False


def test_an_image_placed_above_the_generated_one_drops_the_cover() -> None:
    """`_lead_image` is positional, so a new first picture breaks the pairing."""
    doc = _doc_with_lead()
    doc["content"].insert(0, {"type": "image", "attrs": {"src": "/api/media/99/a.png"}})

    card = derive_card("Headline", doc, Verdict.MIXED, generated=_generated())

    assert card.image == "/api/media/99/a.png"
    assert card.image_is_generated_cover is False


def test_a_cover_the_store_did_not_write_is_refused() -> None:
    """The cover is not in the document, so `assert_media_is_ours` never sees it.

    `derive_card` is where it gets its MEDIA_SRC_RE check, and without it
    approve-time media validation has a hole exactly the width of the one
    field the reader's feed renders in an <img src>.
    """
    card = derive_card(
        "Headline",
        _doc_with_lead(),
        Verdict.MIXED,
        generated=_generated(cover="https://evil.example/tracker.gif"),
    )

    assert card.image == _GEN_LEAD
    assert card.image_is_generated_cover is False


def test_an_article_with_no_generated_imagery_derives_exactly_as_before() -> None:
    """The default-argument regression, for every article written before this."""
    doc = _doc_with_lead()

    assert derive_card("Headline", doc, Verdict.MIXED, generated=None).image == _GEN_LEAD
    assert derive_card("Headline", doc, Verdict.MIXED).image == _GEN_LEAD


def test_a_malformed_generated_column_is_ignored_rather_than_raising() -> None:
    """The column is JSONB and could be hand-edited; a card must still derive."""
    for broken in ({}, {"lead": None, "cover": None}, {"cover": {"src": _GEN_COVER}}):
        card = derive_card("Headline", _doc_with_lead(), Verdict.MIXED, generated=broken)
        assert card.image == _GEN_LEAD
        assert card.image_is_generated_cover is False


def test_a_handle_listed_but_never_written_is_not_a_body_citation() -> None:
    """The union and the body handle set must stay distinguishable.

    `all_cited_handles` is right for the hallucination and resolution checks —
    a fabricated handle is fabricated wherever it appears. It is wrong for
    `was_cited`, which `FeedService.article` filters the public source list on:
    a handle the model named only in `citations` was shown to the reader as a
    source the article cites, beside prose that never refers to it. Observed on
    a real run — the body cited S2 alone and the page listed three papers.
    """
    output = SynthesisOutput(
        headline="Creatine and strength",
        verdict=Verdict.MIXED,
        summary="One review reports a gain.",
        body=ArticleBody(
            beat_1_claim="Creatine is said to build strength.",
            beat_2_evidence="One meta-analysis found a gain [S2].",
            beat_3_bottom_line="More trials are needed.",
        ),
        citations=[
            CitationGroup(claim="Creatine builds strength", source_ids=["S2", "S4", "S6"])
        ],
    )

    assert output.body.cited_handles() == {"S2"}
    assert output.all_cited_handles() == {"S2", "S4", "S6"}


# ---------------------------------------------------------------------------
# What the structured-output grammar tells the model.
# ---------------------------------------------------------------------------


def test_the_generation_grammar_carries_no_developer_rationale() -> None:
    """Pydantic uses a class docstring as the JSON-schema ``description``, and
    Ollama passes that schema in as the generation grammar. So a docstring
    written to explain a decision to a maintainer is *prompt text* the model
    reads while deciding what to write.

    The docstrings in this module argue for brevity, correctly and at length:
    "every bound here is a ceiling, not a target", "optional and have no
    minimum", "nothing in this system pads to length". Reaching the model, that
    reads as an instruction to keep it short, and two of the first three
    articles carried no sections at all.

    Both concerns are legitimate and they are not the same audience. This pins
    the split: the rationale stays in the docstrings, the shape goes to the
    model.
    """
    schema = SynthesisOutput.model_json_schema()
    rendered = json.dumps(schema)

    for leaked in (
        "DESIGN.md",
        "tiptap.py",
        "derive_card",
        "padding instruction",
        "pads to length",
        "min_length",
        "that is the whole point",
        "validation.py",
    ):
        assert leaked not in rendered, (
            f"{leaked!r} reaches the model inside its own output grammar; it is "
            "written for a maintainer, not for the model"
        )


def test_the_grammar_does_not_call_sections_optional() -> None:
    """`sections` has no `min_length` and must never grow one — that would be
    the padding instruction §6 refuses. But the *word* the model reads is a
    separate choice, and "Optional" told it, inside its own grammar, that
    skipping them was the easy correct answer."""
    sections = SynthesisOutput.model_json_schema()["$defs"]["ArticleBody"][
        "properties"
    ]["sections"]

    assert "min_length" not in sections and "minItems" not in sections
    assert "ptional" not in sections["description"]
    # It still states the low end honestly, so thin evidence yields no sections.
    assert "none at all" in sections["description"]
