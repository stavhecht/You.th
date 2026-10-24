"""Parsing MeSH out of real efetch XML.

``tests/unitTest/fixtures/pubmed_mesh_sample.xml`` was **captured from a live efetch** on
2026-09-04, not hand-written. That matters: the whole window design rests on
``<PubMedPubDate PubStatus="entrez">`` being present, and a hand-made fixture
would only prove that the parser reads the XML someone imagined.

Five records, chosen to cover the cases the parser has to get right:

===========  ==========================================================
PMID         why it is in the fixture
===========  ==========================================================
42690769     18 headings, 9 chemicals — the ordinary rich case
42693329     an RCT carrying /administration & dosage and /therapeutic use
42669527     an RCT whose ChemicalList contains D003401 (creatine)
42028948     **no abstract**, 7 headings, 1 chemical — the record the
             pipeline's own parser throws away
42682565     not yet indexed: no MeSH at all
===========  ==========================================================
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from xml.etree import ElementTree

import pytest

from app.discovery.harvest import parse_mesh_record
from app.domain.enums import StudyType
from app.retrieval.pubmed import PubMedProvider

FIXTURE = Path(__file__).parent / "fixtures" / "pubmed_mesh_sample.xml"


@pytest.fixture(scope="module")
def articles() -> dict[str, ElementTree.Element]:
    root = ElementTree.parse(FIXTURE).getroot()
    return {
        element.findtext(".//PMID") or "": element
        for element in root.findall(".//PubmedArticle")
    }


def test_fixture_holds_the_five_records(articles: dict[str, ElementTree.Element]) -> None:
    assert set(articles) == {
        "42690769",
        "42693329",
        "42669527",
        "42028948",
        "42682565",
    }


# --- the case this parser exists for ----------------------------------------


def test_an_abstract_less_record_is_kept(articles: dict[str, ElementTree.Element]) -> None:
    """The entire justification for not reusing ``CandidatePaper``.

    A MEDLINE record with no abstract still carries its full heading list, which
    is the whole signal here. There were 21,689 such records in the supplements
    and vitamins nets when this fixture was captured — not an edge case.
    """
    record = parse_mesh_record(articles["42028948"])
    assert record is not None
    assert len(record.descriptors) == 7
    assert any(hit.in_chemical_list for hit in record.descriptors)


def test_the_pipeline_parser_still_drops_it(
    articles: dict[str, ElementTree.Element],
) -> None:
    """The other half of the same fact, pinned against the same bytes.

    If this ever starts returning a paper, ``ScholarlyProvider``'s documented
    "drop records with an empty abstract" has changed and the argument in
    ``harvest.py``'s docstring needs revisiting — the two parsers exist because
    they disagree here, and nowhere else.
    """
    provider = PubMedProvider.__new__(PubMedProvider)
    assert provider._parse_article(articles["42028948"]) is None
    # And the shared path is otherwise untouched by this package.
    assert provider._parse_article(articles["42669527"]) is not None


def test_an_unindexed_record_is_skipped(
    articles: dict[str, ElementTree.Element],
) -> None:
    """No MeSH means nothing to count — not an error.

    It is dropped here rather than filtered in the esearch because a record that
    gains its headings next month is picked up by the window overlap, which is
    the case that design exists for.
    """
    assert parse_mesh_record(articles["42682565"]) is None


# --- the fields ---------------------------------------------------------------


def test_entrez_date_is_read(articles: dict[str, ElementTree.Element]) -> None:
    """The bucketing key. Present on all five real records."""
    assert parse_mesh_record(articles["42693329"]).entrez_date == date(2026, 9, 4)
    assert parse_mesh_record(articles["42669527"]).entrez_date == date(2026, 8, 30)
    assert parse_mesh_record(articles["42028948"]).entrez_date == date(2026, 4, 24)


def test_chemicals_are_flagged_by_ui(articles: dict[str, ElementTree.Element]) -> None:
    """``<ChemicalList>`` is a sibling of the headings, so it joins on UI.

    D003401 is creatine — the example the schema's dedup argument is written
    around, appearing here in a real record.
    """
    record = parse_mesh_record(articles["42669527"])
    hits = {hit.ui: hit for hit in record.descriptors}
    assert hits["D003401"].in_chemical_list is True
    assert hits["D003401"].name == "Creatine"


def test_intervention_qualifiers_are_flagged(
    articles: dict[str, ElementTree.Element],
) -> None:
    """The signal that separates something given from something measured."""
    record = parse_mesh_record(articles["42693329"])
    assert any(hit.intervention_qualifier for hit in record.descriptors)


def test_major_topic_is_read(articles: dict[str, ElementTree.Element]) -> None:
    """The indexer's judgement that the paper is *about* this."""
    record = parse_mesh_record(articles["42690769"])
    assert any(hit.major_topic for hit in record.descriptors)
    assert not all(hit.major_topic for hit in record.descriptors)


def test_study_type_comes_from_publication_tags(
    articles: dict[str, ElementTree.Element],
) -> None:
    """Graded with the pipeline's own classifier, so the two cannot drift."""
    assert parse_mesh_record(articles["42669527"]).study_type is StudyType.RCT
    # Tagged 'Journal Article' + 'Review' — a real narrative review, from tags
    # alone. Its title also contains no study-design word, which is the point of
    # the next test.
    assert parse_mesh_record(articles["42028948"]).study_type is StudyType.NARRATIVE_REVIEW


def test_grading_never_reads_the_title() -> None:
    """Publication tags only — the prose fallback must not run here.

    ``classify_study_type`` guesses from title and abstract when the tags
    identify nothing, and that fallback exists for OpenAlex and Semantic
    Scholar, which supply no publication types. PubMed always does, so on this
    path the fallback could only ever guess from a headline — the same trap
    CLAUDE.md records as having once graded a plainly-tagged review as a
    systematic review.

    Asserted by construction: a record whose tags say nothing must come back
    UNKNOWN however suggestive its title is.
    """
    xml = (
        "<PubmedArticle><MedlineCitation><PMID>1</PMID>"
        "<Article><ArticleTitle>A randomized controlled trial and meta-analysis"
        " of creatine</ArticleTitle></Article>"
        '<MeshHeadingList><MeshHeading><DescriptorName UI="D003401">Creatine'
        "</DescriptorName></MeshHeading></MeshHeadingList>"
        "<PublicationTypeList><PublicationType>Journal Article</PublicationType>"
        "</PublicationTypeList></MedlineCitation><PubmedData><History>"
        '<PubMedPubDate PubStatus="entrez"><Year>2026</Year><Month>9</Month>'
        "<Day>4</Day></PubMedPubDate></History></PubmedData></PubmedArticle>"
    )
    record = parse_mesh_record(ElementTree.fromstring(xml))
    assert record is not None
    assert record.study_type is StudyType.UNKNOWN


def test_every_descriptor_has_a_ui_and_a_name(
    articles: dict[str, ElementTree.Element],
) -> None:
    for pmid in ("42690769", "42693329", "42669527", "42028948"):
        record = parse_mesh_record(articles[pmid])
        assert record is not None
        for hit in record.descriptors:
            assert hit.ui and hit.name


# --- malformed input ----------------------------------------------------------


@pytest.mark.parametrize(
    "xml",
    [
        # No PMID.
        "<PubmedArticle><MedlineCitation><MeshHeadingList><MeshHeading>"
        '<DescriptorName UI="D003401">Creatine</DescriptorName>'
        "</MeshHeading></MeshHeadingList></MedlineCitation></PubmedArticle>",
        # A malformed Entrez date. Dropped rather than raised: one missing
        # observation, and the overlap re-reads it next scan.
        "<PubmedArticle><MedlineCitation><PMID>1</PMID><MeshHeadingList><MeshHeading>"
        '<DescriptorName UI="D003401">Creatine</DescriptorName></MeshHeading>'
        "</MeshHeadingList></MedlineCitation><PubmedData><History>"
        '<PubMedPubDate PubStatus="entrez"><Year>2026</Year><Month>13</Month>'
        "<Day>45</Day></PubMedPubDate></History></PubmedData></PubmedArticle>",
        # Headings present but none usable.
        "<PubmedArticle><MedlineCitation><PMID>1</PMID><MeshHeadingList><MeshHeading>"
        "<DescriptorName>No UI</DescriptorName></MeshHeading></MeshHeadingList>"
        "</MedlineCitation><PubmedData><History>"
        '<PubMedPubDate PubStatus="entrez"><Year>2026</Year><Month>9</Month>'
        "<Day>4</Day></PubMedPubDate></History></PubmedData></PubmedArticle>",
    ],
)
def test_unusable_records_are_dropped_not_raised(xml: str) -> None:
    assert parse_mesh_record(ElementTree.fromstring(xml)) is None
