"""Europe PMC identifier lookups: a PMID is taken only when a record's own DOI
matches, and never when one DOI answers to two PMIDs."""

from __future__ import annotations

from typing import Any

import httpx

from app.retrieval.identifiers import EuropePMCIdentifiers


class _Http:
    def __init__(self, results: list[dict[str, Any]], status: int = 200) -> None:
        self._results = results
        self._status = status
        self.queries: list[str] = []

    async def get(self, url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        self.queries.append((params or {}).get("query", ""))
        return httpx.Response(
            self._status,
            json={"resultList": {"result": self._results}},
            request=httpx.Request("GET", url),
        )


async def test_a_doi_resolves_to_the_pmid_its_record_carries() -> None:
    http = _Http([{"pmid": "111", "doi": "10.1/ABC", "pmcid": "PMC9", "isOpenAccess": "Y"}])

    found = await EuropePMCIdentifiers(http).pmids_for_dois(["10.1/Abc"])

    assert found == {"10.1/abc": "111"}
    assert http.queries == ['DOI:"10.1/abc"']


async def test_a_record_for_some_other_doi_is_ignored() -> None:
    """A phrase query can match loosely; only the record's own DOI counts."""
    http = _Http([{"pmid": "111", "doi": "10.1/abcd"}])

    assert await EuropePMCIdentifiers(http).pmids_for_dois(["10.1/abc"]) == {}


async def test_a_doi_with_two_pmids_is_left_unresolved() -> None:
    """Guessing would merge two papers in union-find and delete one."""
    http = _Http([{"pmid": "111", "doi": "10.1/abc"}, {"pmid": "222", "doi": "10.1/abc"}])

    assert await EuropePMCIdentifiers(http).pmids_for_dois(["10.1/abc"]) == {}


async def test_a_record_with_no_pmid_resolves_nothing() -> None:
    http = _Http([{"doi": "10.1/abc", "pmcid": "PMC9", "source": "PMC"}])

    assert await EuropePMCIdentifiers(http).pmids_for_dois(["10.1/abc"]) == {}


async def test_pmids_and_dois_share_one_request() -> None:
    http = _Http([])

    await EuropePMCIdentifiers(http).records(pmids=["111"], dois=["10.1/abc"])

    assert http.queries == ['(SRC:MED AND EXT_ID:111) OR DOI:"10.1/abc"']
