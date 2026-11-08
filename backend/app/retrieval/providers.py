"""Europe PMC, Semantic Scholar and OpenAlex providers.

Grouped in one module because they share a shape — single REST call, JSON
response, map to CandidatePaper — unlike PubMed's two-call XML protocol, which
has its own module.

All three are Phase 4 work; ``settings.enabled_providers`` defaults to PubMed
alone. Role of each (DESIGN.md §4):

* **Europe PMC** — breadth, plus the open-access full text the v2 pass needs.
  Also the failover when PubMed rate-limits, since it indexes much of the same
  corpus.
* **Semantic Scholar** — citation counts and cross-domain coverage.
* **OpenAlex** — broad backstop. Fills gaps rather than leading: its metadata is
  the least reliable for study-type classification.
"""

from __future__ import annotations

import logging
import re
from typing import Any, cast

import httpx

from app.domain.contracts import CandidatePaper
from app.domain.enums import SourceApi
from app.evidence.grading import classify_study_type
from app.retrieval.base import ProviderError, RateLimited, SearchQuery
from app.retrieval.throttle import HttpClient, retry_after_seconds

logger = logging.getLogger(__name__)

EUROPE_PMC_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
SEMANTIC_SCHOLAR_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
OPENALEX_URL = "https://api.openalex.org/works"

MIN_ABSTRACT_CHARS = 40


async def _get_json(
    http: HttpClient,
    url: str,
    params: dict[str, Any],
    provider: str,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    try:
        response = await http.get(url, params=params, headers=headers)
    except httpx.HTTPError as exc:
        raise ProviderError(f"{provider} transport failure: {exc}") from exc

    if response.status_code == 429:
        # Reached only when the client wrapping this ran out of retries, or
        # when the provider was built against a bare httpx client.
        raise RateLimited(
            f"{provider} rate limit",
            retry_after=retry_after_seconds(response) or None,
        )
    if response.status_code >= 400:
        raise ProviderError(f"{provider} returned {response.status_code}")

    try:
        return cast(dict[str, Any], response.json())
    except ValueError as exc:
        raise ProviderError(f"{provider} returned non-JSON") from exc


def _clean_doi(doi: str | None) -> str | None:
    """Strip the URL prefixes providers inconsistently attach.

    Matters for dedup: 'https://doi.org/10.1/x' and '10.1/x' are the same paper
    but different dedup keys, so the same study would enter the prompt twice
    under two handles.
    """
    if not doi:
        return None
    cleaned = doi.strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if cleaned.lower().startswith(prefix):
            cleaned = cleaned[len(prefix) :]
    return cleaned or None


#: PubMed field tags in the pipeline's terms, which Europe PMC does not use.
_PUBMED_FIELD_TAG_RE = re.compile(
    r"\[(?:mesh|mh|tiab|ti|ab|pt|majr|sh)(?::noexp)?\]", re.IGNORECASE
)


def europe_pmc_query(terms: str) -> str:
    """PubMed-syntax terms with the field tags removed, for the providers that have none."""
    return " ".join(_PUBMED_FIELD_TAG_RE.sub("", terms).split())


class EuropePMCProvider:
    def __init__(self, http: HttpClient) -> None:
        self._http = http

    @property
    def source_api(self) -> SourceApi:
        return SourceApi.EUROPE_PMC

    async def count(self, query: str) -> int:
        """How many Europe PMC records match ``query`` (its ``hitCount``).

        Used by the research agent to cross-check PubMed's count for a topic.

        Raises:
            RateLimited, ProviderError: a count that could not be made must
                never read as zero papers.
        """
        payload = await _get_json(
            self._http,
            EUROPE_PMC_URL,
            {"query": query, "format": "json", "resultType": "idlist", "pageSize": 1},
            "europe_pmc",
        )
        hits = payload.get("hitCount")
        if not isinstance(hits, int):
            raise ProviderError("europe_pmc returned no hitCount")
        return hits

    async def search(self, query: SearchQuery) -> list[CandidatePaper]:
        """Title-and-abstract search, never full text.

        The pipeline's terms are PubMed syntax. Europe PMC has no ``[MeSH]`` or
        ``[tiab]`` tags (it ignores them and searches the phrase), so they are
        stripped, and the query is scoped to ``TITLE_ABS`` — Europe PMC's default
        searches the *full text* of open-access papers. Measured 2026-10-01:
        creatine + sleep matched 13,693 papers in full text, led by brain-imaging
        studies where creatine is a measured metabolite, against 336 in title
        and abstract. An empty scoped search used to retry in full text, for
        "zone 2 training" (0 against 20); with names searched as words too it
        finds 13 scoped, and the retry filled the wellness suite's ranked lists
        with passing mentions (2026-11-08). Empty is the honest answer.
        """
        plain = europe_pmc_query(query.terms)
        payload = await self._search(f"TITLE_ABS:({plain})", query)

        papers: list[CandidatePaper] = []
        for record in payload.get("resultList", {}).get("result", []):
            abstract = (record.get("abstractText") or "").strip()
            title = (record.get("title") or "").strip()
            pmid = (record.get("pmid") or "").strip() or None
            doi = _clean_doi(record.get("doi"))

            if len(abstract) < MIN_ABSTRACT_CHARS or not title or (not pmid and not doi):
                continue

            publication_types = [
                entry
                for entry in (record.get("pubTypeList", {}) or {}).get("pubType", [])
                if isinstance(entry, str)
            ]
            year = record.get("pubYear")

            papers.append(
                CandidatePaper(
                    pmid=pmid,
                    doi=doi,
                    title=title,
                    abstract=abstract,
                    journal=(record.get("journalTitle") or None),
                    year=int(year) if str(year).isdigit() else None,
                    study_type=classify_study_type(publication_types, title, abstract),
                    raw_study_type="; ".join(publication_types) or None,
                    citation_count=record.get("citedByCount"),
                    url=(
                        f"https://europepmc.org/article/"
                        f"{record.get('source', 'MED')}/{record.get('id')}"
                    ),
                    source_api=SourceApi.EUROPE_PMC,
                )
            )
        return papers

    async def _search(self, terms: str, query: SearchQuery) -> dict[str, Any]:
        if query.reviews_only:
            terms = f'({terms}) AND PUB_TYPE:"review"'
        if query.min_year:
            terms = f"({terms}) AND PUB_YEAR:[{query.min_year} TO 3000]"
        return await _get_json(
            self._http,
            EUROPE_PMC_URL,
            {
                "query": terms,
                "format": "json",
                # 'core' is what includes abstracts; the default result type
                # omits them, which makes every record unusable here.
                "resultType": "core",
                "pageSize": query.max_results,
            },
            "europe_pmc",
        )


class SemanticScholarProvider:
    def __init__(self, http: HttpClient, api_key: str | None = None) -> None:
        self._http = http
        self._api_key = api_key

    @property
    def source_api(self) -> SourceApi:
        return SourceApi.SEMANTIC_SCHOLAR

    async def search(self, query: SearchQuery) -> list[CandidatePaper]:
        if not self._api_key:
            # Defence in depth: ``retrieval/factory.py`` does not build this
            # provider without a key, precisely so this branch stays
            # unreachable. Returning an empty list from a search that never
            # ran is indistinguishable downstream from a search that found
            # nothing, and RetrieveStage's coverage check would count it as
            # proof the claim was searched. Loud, because reaching it means
            # the provider was wired somewhere else.
            logger.warning("semantic scholar: no API key; returning no results")
            return []

        params: dict[str, Any] = {
            "query": query.terms,
            "limit": min(query.max_results, 100),
            "fields": "title,abstract,year,venue,citationCount,externalIds,publicationTypes",
        }
        if query.min_year:
            params["year"] = f"{query.min_year}-"

        payload = await _get_json(
            self._http,
            SEMANTIC_SCHOLAR_URL,
            params,
            "semantic_scholar",
            headers={"x-api-key": self._api_key},
        )

        papers: list[CandidatePaper] = []
        for record in payload.get("data", []):
            abstract = (record.get("abstract") or "").strip()
            title = (record.get("title") or "").strip()
            # Identifiers live under externalIds, not at the top level.
            external = record.get("externalIds") or {}
            pmid = str(external.get("PubMed")).strip() if external.get("PubMed") else None
            doi = _clean_doi(external.get("DOI"))

            if len(abstract) < MIN_ABSTRACT_CHARS or not title or (not pmid and not doi):
                continue

            publication_types = record.get("publicationTypes") or []
            papers.append(
                CandidatePaper(
                    pmid=pmid,
                    doi=doi,
                    title=title,
                    abstract=abstract,
                    journal=record.get("venue") or None,
                    year=record.get("year"),
                    study_type=classify_study_type(publication_types, title, abstract),
                    raw_study_type="; ".join(publication_types) or None,
                    citation_count=record.get("citationCount"),
                    url=f"https://doi.org/{doi}" if doi else None,
                    source_api=SourceApi.SEMANTIC_SCHOLAR,
                )
            )
        return papers


class OpenAlexProvider:
    def __init__(
        self, http: HttpClient, mailto: str | None = None, api_key: str | None = None
    ) -> None:
        self._http = http
        # OpenAlex gives the polite pool — materially better latency — in
        # exchange for a contact address. Worth setting.
        self._mailto = mailto
        # Since 2026 keyless requests draw on a small free budget shared by
        # everyone on the IP; measured 2026-10-01, it ran out after about 100
        # searches and every later call was a 429 until midnight UTC. A key
        # (free) carries its own budget.
        self._api_key = api_key

    @property
    def source_api(self) -> SourceApi:
        return SourceApi.OPENALEX

    async def search(self, query: SearchQuery) -> list[CandidatePaper]:
        filters = ["has_abstract:true"]
        if query.min_year:
            filters.append(f"from_publication_date:{query.min_year}-01-01")
        if query.reviews_only:
            filters.append("type:review")
        # Tagged terms wreck its search ("alcohol reduction" and blood pressure: 4 unrelated
        # papers against 8,407, 2026-11-07), and plain ``search`` also reads full text
        # ("bulletproof coffee": 497 works with gun adverts, 5 in title and abstract,
        # 2026-11-08). A comma would end the filter, and MeSH names carry them.
        plain = europe_pmc_query(query.terms).replace(",", " ")
        filters.append(f"title_and_abstract.search:{' '.join(plain.split())}")

        params: dict[str, Any] = {
            "filter": ",".join(filters),
            "sort": "relevance_score:desc",
            "per-page": min(query.max_results, 50),
        }
        if self._mailto:
            params["mailto"] = self._mailto
        if self._api_key:
            params["api_key"] = self._api_key

        payload = await _get_json(self._http, OPENALEX_URL, params, "openalex")

        papers: list[CandidatePaper] = []
        for record in payload.get("results", []):
            abstract = _reconstruct_abstract(record.get("abstract_inverted_index"))
            title = (record.get("display_name") or "").strip()
            ids = record.get("ids") or {}
            doi = _clean_doi(ids.get("doi"))
            pmid = None
            if pubmed_url := ids.get("pmid"):
                pmid = str(pubmed_url).rstrip("/").rsplit("/", 1)[-1] or None

            if len(abstract) < MIN_ABSTRACT_CHARS or not title or (not pmid and not doi):
                continue

            work_type = record.get("type")
            publication_types = [work_type] if isinstance(work_type, str) else []
            venue = ((record.get("primary_location") or {}).get("source") or {}).get(
                "display_name"
            )

            papers.append(
                CandidatePaper(
                    pmid=pmid,
                    doi=doi,
                    title=title,
                    abstract=abstract,
                    journal=venue,
                    year=record.get("publication_year"),
                    study_type=classify_study_type(publication_types, title, abstract),
                    raw_study_type=work_type if isinstance(work_type, str) else None,
                    citation_count=record.get("cited_by_count"),
                    url=f"https://doi.org/{doi}" if doi else None,
                    source_api=SourceApi.OPENALEX,
                )
            )
        return papers


def _reconstruct_abstract(inverted_index: dict[str, list[int]] | None) -> str:
    """Rebuild an abstract from OpenAlex's token -> positions map.

    OpenAlex does not ship abstracts as strings; it ships an inverted index.
    Records that reconstruct to nothing are dropped by the caller.
    """
    if not inverted_index:
        return ""
    positions: list[tuple[int, str]] = [
        (position, token)
        for token, token_positions in inverted_index.items()
        for position in token_positions
    ]
    positions.sort()
    return " ".join(token for _, token in positions).strip()
