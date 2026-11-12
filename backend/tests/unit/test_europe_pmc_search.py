"""Europe PMC and OpenAlex as retrieval providers: PubMed-syntax terms without the
tags, searched in title and abstract only, never in full text."""

from __future__ import annotations

from typing import Any

import httpx

from app.retrieval.base import SearchQuery
from app.retrieval.providers import EuropePMCProvider, OpenAlexProvider, europe_pmc_query

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


def test_the_safety_subheading_tag_is_removed_too() -> None:
    terms = '"adverse effects"[sh] OR safety[tiab]'
    assert europe_pmc_query(terms) == '"adverse effects" OR safety'


async def test_openalex_searches_title_and_abstract_without_pubmed_tags() -> None:
    """Tagged, "alcohol reduction" and blood pressure found 4 unrelated papers on OpenAlex;
    plain, 8,407, led by the meta-analysis the question needed (2026-11-07). Its plain
    ``search`` also reads full text: "bulletproof coffee" matched 497 works, gun adverts
    among them, against 5 in title and abstract, all about the drink (2026-11-08)."""
    sent: list[dict[str, Any]] = []

    class _Recording:
        async def get(
            self, url: str, *, params: dict[str, Any] | None = None, headers: Any = None
        ) -> httpx.Response:
            sent.append(dict(params or {}))
            return httpx.Response(200, json={"results": []})

    await OpenAlexProvider(_Recording()).search(_query(reviews_only=True))

    [params] = sent
    assert "search" not in params
    assert params["filter"] == (
        f"has_abstract:true,type:review,title_and_abstract.search:{europe_pmc_query(TERMS)}"
    )
    assert params["sort"] == "relevance_score:desc"


async def test_a_comma_cannot_end_the_openalex_filter_early() -> None:
    """A filter list is comma-separated, and MeSH names carry commas."""
    sent: list[str] = []

    class _Recording:
        async def get(
            self, url: str, *, params: dict[str, Any] | None = None, headers: Any = None
        ) -> httpx.Response:
            sent.append((params or {})["filter"])
            return httpx.Response(200, json={"results": []})

    terms = '"stress, psychological"[MeSH] AND yoga'
    await OpenAlexProvider(_Recording()).search(SearchQuery(claim="x", terms=terms))

    assert sent == [
        'has_abstract:true,title_and_abstract.search:"stress psychological" AND yoga'
    ]


async def test_the_search_is_scoped_to_title_and_abstract() -> None:
    http = _Http({"resultList": {"result": [RECORD]}})

    papers = await EuropePMCProvider(http).search(_query(reviews_only=True, min_year=2015))

    assert len(papers) == 1
    assert http.queries == [
        f'((TITLE_ABS:({europe_pmc_query(TERMS)})) AND PUB_TYPE:"review") '
        "AND PUB_YEAR:[2015 TO 3000]"
    ]


async def test_an_empty_scoped_search_is_not_retried_in_full_text() -> None:
    """Full text answered with passing mentions: "under-desk treadmills" is in no title or
    abstract, and its 20 full-text hits filled the ranked list with sit-stand desks and
    sedentary-behaviour reviews (2026-11-08)."""
    http = _Http({"resultList": {"result": []}})

    papers = await EuropePMCProvider(http).search(_query())

    assert papers == []
    assert http.queries == [f"TITLE_ABS:({europe_pmc_query(TERMS)})"]
