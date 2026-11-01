"""Cross-provider identity — the failure that only appears at Phase 4.

With PubMed alone every record carries both identifiers, so keying on one of
them works and nothing here can fail. Turn on Europe PMC, Semantic Scholar and
OpenAlex and the same study starts arriving with three different identifier
*subsets*, which the old single-key approach could not unify.

What that produced is the specific harm this product exists to prevent: one
study, entering the prompt three times under three handles, cited as three
independent findings. ``rerank.assign_handles`` guards the same failure from
the other side. These tests guard this side.
"""

from __future__ import annotations

import pytest

from app.domain.contracts import CandidatePaper
from app.domain.enums import SourceApi, StudyType
from app.retrieval.dedup import (
    identity_keys,
    merge_candidates,
    merge_group,
    unique_papers,
)


def _paper(
    api: SourceApi,
    *,
    pmid: str | None = None,
    doi: str | None = None,
    title: str = "Ashwagandha and stress",
    abstract: str | None = None,
    study_type: StudyType = StudyType.UNKNOWN,
    citation_count: int | None = None,
    year: int | None = None,
    journal: str | None = None,
) -> CandidatePaper:
    return CandidatePaper(
        pmid=pmid,
        doi=doi,
        title=title,
        abstract=abstract or f"An abstract from {api} long enough to be real.",
        journal=journal,
        year=year,
        study_type=study_type,
        citation_count=citation_count,
        source_api=api,
    )


# ---------------------------------------------------------------------------
# Union-find: a record carrying both identifiers bridges two groups.
# ---------------------------------------------------------------------------


def test_a_record_carrying_both_ids_unifies_the_two_partial_records() -> None:
    """The Phase-4 scenario, and the whole reason this module exists.

    Europe PMC knows the PMID, Semantic Scholar knows the DOI, PubMed knows
    both. Keyed one-at-a-time these are three papers; the PubMed record is what
    proves they are one.
    """
    raw = {
        "reduces stress": [
            _paper(SourceApi.EUROPE_PMC, pmid="123"),
            _paper(SourceApi.SEMANTIC_SCHOLAR, doi="10.1/x"),
            _paper(SourceApi.PUBMED, pmid="123", doi="10.1/x"),
        ]
    }

    merged = merge_candidates(raw)["reduces stress"]

    assert len(merged) == 1
    assert (merged[0].pmid, merged[0].doi) == ("123", "10.1/x")


def test_without_a_bridging_record_partial_records_stay_separate() -> None:
    """The documented limit of identifier-only union-find.

    Asserted rather than left implicit: this is the case a title bridge would
    catch, and the decision not to bridge on title is deliberate — an erratum
    notice repeats its parent's title verbatim, and merging those silently
    deletes a real study.
    """
    raw = {
        "reduces stress": [
            _paper(SourceApi.EUROPE_PMC, pmid="123"),
            _paper(SourceApi.SEMANTIC_SCHOLAR, doi="10.1/x"),
        ]
    }

    assert len(merge_candidates(raw)["reduces stress"]) == 2


def test_an_identical_title_never_joins_two_papers_with_distinct_ids() -> None:
    """Two studies can share a title. They are not one paper."""
    raw = {
        "c": [
            _paper(SourceApi.PUBMED, pmid="1", title="Ashwagandha and stress"),
            _paper(SourceApi.PUBMED, pmid="2", title="Ashwagandha and stress"),
        ]
    }

    assert len(merge_candidates(raw)["c"]) == 2


def test_dois_unify_case_insensitively() -> None:
    """The DB index is on ``lower(doi)``; grouping has to agree with it."""
    raw = {
        "c": [
            _paper(SourceApi.PUBMED, doi="10.1/ABC"),
            _paper(SourceApi.OPENALEX, doi="10.1/abc"),
        ]
    }

    assert len(merge_candidates(raw)["c"]) == 1


def test_a_chain_of_records_collapses_transitively() -> None:
    """Union-find, not pairwise matching: A-B and B-C implies A-C."""
    raw = {
        "c": [
            _paper(SourceApi.EUROPE_PMC, pmid="1"),
            _paper(SourceApi.PUBMED, pmid="1", doi="10.1/a"),
            _paper(SourceApi.OPENALEX, doi="10.1/a"),
        ]
    }

    assert len(merge_candidates(raw)["c"]) == 1


# ---------------------------------------------------------------------------
# Identity must be shared across claims, not merged per claim.
# ---------------------------------------------------------------------------


def test_a_paper_answering_two_claims_is_the_same_object_in_both() -> None:
    """Everything downstream keys on ``dedup_key``.

    If a paper were merged under one claim but not another it would carry two
    different keys through the pipeline, and RankStage's id lookup would drop
    it from one of them.
    """
    raw = {
        "reduces stress": [_paper(SourceApi.EUROPE_PMC, pmid="9")],
        "improves sleep": [
            _paper(SourceApi.OPENALEX, doi="10.9/z"),
            _paper(SourceApi.PUBMED, pmid="9", doi="10.9/z"),
        ],
    }

    merged = merge_candidates(raw)

    assert len(merged["improves sleep"]) == 1
    assert merged["reduces stress"][0].dedup_key == merged["improves sleep"][0].dedup_key
    assert unique_papers(merged) == [merged["reduces stress"][0]]


def test_claim_order_and_membership_are_preserved() -> None:
    """Merging must not silently drop a claim or reorder its candidates."""
    raw = {
        "a": [_paper(SourceApi.PUBMED, pmid="1"), _paper(SourceApi.PUBMED, pmid="2")],
        "b": [_paper(SourceApi.PUBMED, pmid="2")],
        "c": [],
    }

    merged = merge_candidates(raw)

    assert list(merged) == ["a", "b", "c"]
    assert [p.pmid for p in merged["a"]] == ["1", "2"]
    assert merged["c"] == []


# ---------------------------------------------------------------------------
# What survives a merge.
# ---------------------------------------------------------------------------


def test_the_cleanest_abstract_wins_not_the_longest() -> None:
    """OpenAlex abstracts are rebuilt from an inverted index.

    Lost punctuation and tokenisation artefacts are normal there, and the
    reconstruction is often *longer* than the clean original. That text is both
    what gets embedded and what the model reads as evidence, so provider trust
    has to outrank length.
    """
    group = [
        _paper(SourceApi.OPENALEX, doi="10.1/a", abstract="reconstructed text " * 20),
        _paper(SourceApi.PUBMED, doi="10.1/a", abstract="Clean PubMed abstract."),
    ]

    merged = merge_group(group)

    assert merged.abstract == "Clean PubMed abstract."
    # Provenance names the provider whose text was actually kept.
    assert merged.source_api is SourceApi.PUBMED


def test_length_still_breaks_a_tie_within_one_provider() -> None:
    group = [
        _paper(SourceApi.OPENALEX, doi="10.1/a", abstract="Short one here."),
        _paper(SourceApi.OPENALEX, doi="10.1/a", abstract="A considerably longer one."),
    ]

    assert merge_group(group).abstract == "A considerably longer one."


def test_the_strongest_study_type_wins() -> None:
    """One provider knowing a paper is an RCT is informative.

    Another provider not knowing is not — so the merge must not let an UNKNOWN
    classification pull a gradeable source down into the weakest tier, where it
    would cap the verdict at 'weak'.
    """
    group = [
        _paper(SourceApi.OPENALEX, doi="10.1/a", study_type=StudyType.UNKNOWN),
        _paper(SourceApi.PUBMED, doi="10.1/a", study_type=StudyType.RCT),
        _paper(SourceApi.EUROPE_PMC, doi="10.1/a", study_type=StudyType.OBSERVATIONAL),
    ]

    assert merge_group(group).study_type is StudyType.RCT


def test_metadata_is_filled_in_from_whichever_record_had_it() -> None:
    """Each provider knows a different subset; the merge is their union."""
    group = [
        _paper(SourceApi.PUBMED, pmid="1", doi="10.1/a", year=2021),
        _paper(
            SourceApi.SEMANTIC_SCHOLAR,
            doi="10.1/a",
            citation_count=42,
            journal="J. Clin. Nutr.",
        ),
    ]

    merged = merge_group(group)

    assert (merged.year, merged.citation_count, merged.journal) == (
        2021,
        42,
        "J. Clin. Nutr.",
    )


def test_a_lone_record_is_returned_untouched() -> None:
    paper = _paper(SourceApi.PUBMED, pmid="1")
    assert merge_group([paper]) is paper


# ---------------------------------------------------------------------------
# Identity keys.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("paper", "expected"),
    [
        (_paper(SourceApi.PUBMED, pmid="1", doi="10.1/A"), ["doi:10.1/a", "pmid:1"]),
        (_paper(SourceApi.PUBMED, pmid="1"), ["pmid:1"]),
        (_paper(SourceApi.PUBMED, doi="10.1/A"), ["doi:10.1/a"]),
    ],
)
def test_identity_keys_lists_every_identifier_not_the_best_one(
    paper: CandidatePaper, expected: list[str]
) -> None:
    """Carrying both is exactly what lets a record bridge two groups."""
    assert identity_keys(paper) == expected
