"""The retraction checker, and the three-state verdict it exists for.

The interesting tests here are not "does it spot a retraction" — that is one
field lookup. They are the ones that pin **unchecked apart from clean**. Every
way this feature fails quietly runs through that distinction: a provider that
times out, a PMID that does not resolve, a schema change. All of them produce
the same absence of a positive result as a genuinely clean paper, and treating
them alike writes a ``retraction_checked_at`` that suppresses the next sweep
too — so the error compounds instead of being retried.

One of these tests exists because the live probe caught the bug. NCBI does not
omit an unresolvable id; it returns a record carrying ``{"error": ...}`` and no
``pubtype``, which the first version read as an empty type list and reported as
verified clean.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx
import pytest

from app.evidence.grading import StudyType, classify_study_type, is_retracted
from app.retrieval.retractions import (
    UNCHECKED,
    CrossrefRetractionSource,
    PaperIdentity,
    PubMedRetractionSource,
    RetractionChecker,
    RetractionVerdict,
)


class _Http:
    """Scripted responses keyed by a substring of the URL."""

    def __init__(self, routes: dict[str, Any], *, fail: bool = False) -> None:
        self._routes = routes
        self._fail = fail
        self.calls: list[str] = []

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        self.calls.append(url)
        if self._fail:
            raise httpx.ConnectError("no route to host")
        for fragment, payload in self._routes.items():
            if fragment in url:
                if isinstance(payload, int):
                    return httpx.Response(payload, request=httpx.Request("GET", url))
                return httpx.Response(200, json=payload, request=httpx.Request("GET", url))
        return httpx.Response(404, request=httpx.Request("GET", url))


def _esummary(records: dict[str, Any]) -> dict[str, Any]:
    return {"result": {"uids": list(records), **records}}


def _pubmed(*pubtypes: str, uid: str = "1") -> _Http:
    """A PubMed client that answers for one paper with these publication types."""
    return _Http({"esummary": _esummary({uid: {"pubtype": list(pubtypes)}})})


# ---------------------------------------------------------------------------
# PubMed.
# ---------------------------------------------------------------------------


async def test_pubmed_flags_a_retracted_publication() -> None:
    source = PubMedRetractionSource(_pubmed("Journal Article", "Retracted Publication"))

    verdicts = await source.check([PaperIdentity("s1", pmid="1")])

    assert verdicts["s1"].retracted is True
    assert verdicts["s1"].checked is True
    assert "retracted publication" in verdicts["s1"].detail


async def test_pubmed_reports_a_clean_paper_as_checked_and_clean() -> None:
    http = _pubmed("Journal Article", "Randomized Controlled Trial")

    verdicts = await PubMedRetractionSource(http).check([PaperIdentity("s1", pmid="1")])

    assert verdicts["s1"] == RetractionVerdict(
        checked=True, retracted=False, concern=False, detail="", answered_by=("pubmed",)
    )


async def test_an_unresolvable_pmid_is_unchecked_not_clean() -> None:
    """The bug a live control caught, not review.

    NCBI does not omit an id it cannot resolve — it returns a record for it
    carrying an ``error`` key and no ``pubtype``. Reading that as an empty type
    list marks a paper nobody could look up as verified clean, and then writes
    a ``retraction_checked_at`` that stops the next sweep re-asking.
    """
    http = _Http({"esummary": _esummary({"999": {"error": "cannot get document summary"}})})

    verdicts = await PubMedRetractionSource(http).check([PaperIdentity("s1", pmid="999")])

    assert "s1" not in verdicts, "an unresolvable id must yield no verdict at all"


async def test_a_transport_failure_leaves_the_batch_unchecked() -> None:
    """Not an exception, and not a clean bill of health.

    A sweep that raises on the first dead connection abandons the thousands of
    papers it had already verified; one that returns clean is worse.
    """
    http = _Http({}, fail=True)

    verdicts = await PubMedRetractionSource(http).check(
        [PaperIdentity("s1", pmid="1"), PaperIdentity("s2", pmid="2")]
    )

    assert verdicts == {}


async def test_pubmed_skips_papers_with_no_pmid_without_calling() -> None:
    http = _Http({})

    verdicts = await PubMedRetractionSource(http).check([PaperIdentity("s1", doi="10.1/x")])

    assert verdicts == {}
    assert http.calls == []


async def test_an_expression_of_concern_is_recorded_but_not_a_retraction() -> None:
    """Under investigation, not withdrawn. The paper may yet be exonerated, so
    it stays admissible and the fact is recorded for a human."""
    http = _pubmed("Journal Article", "Expression of Concern")

    verdict = (await PubMedRetractionSource(http).check([PaperIdentity("s1", pmid="1")]))["s1"]

    assert verdict.concern is True
    assert verdict.retracted is False


# ---------------------------------------------------------------------------
# Crossref.
# ---------------------------------------------------------------------------


async def test_crossref_flags_an_update_to_retraction() -> None:
    http = _Http({"api.crossref.org": {"message": {"update-to": [{"type": "retraction"}]}}})

    verdict = (
        await CrossrefRetractionSource(http).check([PaperIdentity("s1", doi="10.1/x")])
    )["s1"]

    assert verdict.retracted is True
    assert verdict.answered_by == ("crossref",)


async def test_crossref_does_not_treat_a_correction_as_a_retraction() -> None:
    """``update-to`` also carries corrections and addenda. A correction is the
    literature working, not failing."""
    http = _Http({"api.crossref.org": {"message": {"update-to": [{"type": "correction"}]}}})

    verdict = (
        await CrossrefRetractionSource(http).check([PaperIdentity("s1", doi="10.1/x")])
    )["s1"]

    assert verdict.checked is True
    assert verdict.retracted is False


async def test_a_doi_crossref_does_not_have_is_unchecked() -> None:
    http = _Http({"api.crossref.org": 404})

    verdicts = await CrossrefRetractionSource(http).check([PaperIdentity("s1", doi="10.1/x")])

    assert verdicts == {}


# ---------------------------------------------------------------------------
# Merging two providers.
# ---------------------------------------------------------------------------


async def test_every_input_paper_appears_in_the_result() -> None:
    """Including the ones nothing could answer for.

    A caller that only saw the answered papers would have to reconstruct the
    difference to know what it failed to check — and the whole point is that
    the gap stays visible without effort.
    """
    checker = RetractionChecker([PubMedRetractionSource(_Http({}, fail=True))])

    verdicts = await checker.check([PaperIdentity("s1", pmid="1"), PaperIdentity("s2")])

    assert set(verdicts) == {"s1", "s2"}
    assert all(v == UNCHECKED for v in verdicts.values())


async def test_a_positive_from_either_provider_wins() -> None:
    """Two bibliographic databases disagreeing is not a tie to break by
    precedence. One of them has newer data, and it is the one saying yes."""
    pubmed = PubMedRetractionSource(_pubmed("Journal Article"))
    crossref = CrossrefRetractionSource(
        _Http({"api.crossref.org": {"message": {"update-to": [{"type": "retraction"}]}}})
    )

    verdict = (
        await RetractionChecker([pubmed, crossref]).check(
            [PaperIdentity("s1", pmid="1", doi="10.1/x")]
        )
    )["s1"]

    assert verdict.retracted is True
    assert verdict.answered_by == ("pubmed", "crossref")


async def test_one_provider_failing_does_not_lose_the_others_answer() -> None:
    """The reason there are two. A paper with no PMID is Crossref's alone, and
    a PubMed outage must not take it down with it."""
    crossref = CrossrefRetractionSource(
        _Http({"api.crossref.org": {"message": {"update-to": [{"type": "retraction"}]}}})
    )

    verdict = (
        await RetractionChecker(
            [PubMedRetractionSource(_Http({}, fail=True)), crossref]
        ).check([PaperIdentity("s1", doi="10.1/x")])
    )["s1"]

    assert verdict.retracted is True
    assert verdict.checked is True


def test_a_checker_with_no_sources_is_refused() -> None:
    """It would answer UNCHECKED for everything, which is technically honest
    and useless — and would look like a working sweep in the logs."""
    with pytest.raises(ValueError):
        RetractionChecker([])


# ---------------------------------------------------------------------------
# The ingest-time predicate, and its agreement with the sweep.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "Retracted Publication",
        "Journal Article; Retracted Publication",
        "retracted publication",
        "Retraction of Publication",
        "  Journal Article ;  Retracted Publication  ",
    ],
)
def test_is_retracted_reads_the_tag_in_any_form(raw: str) -> None:
    assert is_retracted(raw) is True


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "Journal Article",
        "Randomized Controlled Trial; Journal Article",
        # The word appears, the tag does not. A bibliometric study *about*
        # retractions is a perfectly good paper.
        "Review",
    ],
)
def test_is_retracted_does_not_over_match(raw: str | None) -> None:
    assert is_retracted(raw) is False


def test_a_retracted_paper_still_classifies_as_unknown() -> None:
    """The grade cap stays, and is now the second line rather than the only one.

    A row cached before the ingest screen existed is still in the database, so
    the classifier must keep refusing to grade it — the two mechanisms overlap
    deliberately.
    """
    assert (
        classify_study_type(
            ["Journal Article", "Randomized Controlled Trial", "Retracted Publication"],
            title="A trial",
            abstract="A randomized, double-blind, placebo-controlled trial.",
        )
        is StudyType.UNKNOWN
    )


async def test_the_sweep_and_the_ingest_screen_agree_on_what_a_retraction_is() -> None:
    """They read the same frozenset, and this asserts they still do.

    If they diverged, a paper refused at retrieval would be marked clean by the
    sweep — two checks quietly enforcing different rules, with the weaker one
    winning on everything already cached.
    """
    for tag in ("Retracted Publication", "Retraction of Publication"):
        http = _pubmed("Journal Article", tag)
        verdicts = await PubMedRetractionSource(http).check([PaperIdentity("s1", pmid="1")])
        assert verdicts["s1"].retracted is True
        assert is_retracted(f"Journal Article; {tag}") is True
