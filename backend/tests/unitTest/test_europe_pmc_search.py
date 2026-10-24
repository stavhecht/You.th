"""Europe PMC as a retrieval provider: PubMed-syntax terms, scoped to title and
abstract, with a full-text fallback only when that finds nothing."""

from __future__ import annotations

from typing import Any

import httpx

from app.retrieval.base import SearchQuery
from app.retrieval.providers import EuropePMCProvider, europe_pmc_query

TERMS = '(Creatine) AND ("sleep"[MeSH] OR insomnia OR "sleep quality"[tiab])'

RECORD = {
    "id": "123",
    "source": "MED",
    "pmid": "123",
    "doi": "10.1/x",
    "title": "Creatine and sleep: a randomised trial",
    "abstractText": "We randomised forty adults to creatine or placebo and measured sleep.",
    "pubYear": "2024",
    "pubTypeList": {"pubType": ["Randomized Controlled Trial"]},
    "journalTitle": "J Sleep",
}


class _Http:
    def __init__(self, *payloads: dict[str, Any]) -> None:
        self._payloads = list(payloads)
        self.queries: list[str] = []

    async def get(
        self, url: str, *, params: dict[str, Any] | None = None, headers: Any = None
    ) -> httpx.Response:
        self.queries.append((params or {})["query"])
        return httpx.Response(200, json=self._payloads.pop(0))


def _query(**kwargs: Any) -> SearchQuery:
    return SearchQuery(claim="improves sleep", terms=TERMS, **kwargs)


def test_pubmed_field_tags_are_removed() -> None:
    assert europe_pmc_query(TERMS) == '(Creatine) AND ("sleep" OR insomnia OR "sleep quality")'


async def test_the_search_is_scoped_to_title_and_abstract() -> None:
    http = _Http({"resultList": {"result": [RECORD]}})

    papers = await EuropePMCProvider(http).search(_query(reviews_only=True, min_year=2015))

    assert len(papers) == 1
    assert http.queries == [
        f'((TITLE_ABS:({europe_pmc_query(TERMS)})) AND PUB_TYPE:"review") '
        "AND PUB_YEAR:[2015 TO 3000]"
    ]


async def test_an_empty_scoped_search_falls_back_to_full_text_once() -> None:
    http = _Http({"resultList": {"result": []}}, {"resultList": {"result": [RECORD]}})

    papers = await EuropePMCProvider(http).search(_query())

    assert [p.pmid for p in papers] == ["123"]
    assert http.queries == [
        f"TITLE_ABS:({europe_pmc_query(TERMS)})",
        europe_pmc_query(TERMS),
    ]
