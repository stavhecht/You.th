"""The esearch/efetch calls, and the three parameters that make them different.

Discovery's PubMed calls differ from the pipeline's in exactly three ways, and
each is pinned here because each is a silent failure if it regresses:

* ``datetype=edat`` — measure the *indexing* clock, not the journal's
  publication date, which can precede or follow PubMed entry by months.
* day-granularity ``mindate``/``maxdate`` — the pipeline expresses only a year.
* ``sort=date`` — the pipeline sorts by relevance, which for a net this broad
  would make a truncated sweep non-representative in a way nothing reports.

Also pinned: a throttled response must raise rather than return an empty list.
A sweep that reports "no trends this fortnight" because NCBI was rate limiting
is the same class of invisible degradation ``retrieval/throttle.py`` exists to
prevent.

No network — ``respx`` intercepts, and the client is a bare ``httpx.AsyncClient``
so no throttle sleeps run.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.discovery.harvest import EFETCH_BATCH_SIZE, PubMedIndexHarvester
from app.discovery.seeds import SEEDS_BY_NAME
from app.retrieval.base import ProviderError, RateLimited
from app.retrieval.pubmed import EFETCH_URL, ESEARCH_URL

FIXTURE = (Path(__file__).parent / "fixtures" / "pubmed_mesh_sample.xml").read_text()
SEED = SEEDS_BY_NAME["supplements"]


def _esearch_body(count: int, ids: list[str]) -> str:
    return json.dumps({"esearchresult": {"count": str(count), "idlist": ids}})


@pytest.fixture
async def harvester() -> Any:
    async with httpx.AsyncClient() as client:
        yield PubMedIndexHarvester(client)


# --- the parameters that differ from the pipeline's -------------------------


@respx.mock
async def test_esearch_measures_the_indexing_clock(harvester: Any) -> None:
    route = respx.get(ESEARCH_URL).mock(
        return_value=httpx.Response(200, text=_esearch_body(2, ["1", "2"]))
    )

    total, ids = await harvester.count_and_ids(SEED, date(2026, 8, 21), date(2026, 9, 4))

    assert (total, ids) == (2, ["1", "2"])
    params = route.calls.last.request.url.params
    assert params["datetype"] == "edat"
    assert params["mindate"] == "2026/08/21"
    assert params["maxdate"] == "2026/09/04"
    assert params["sort"] == "date"


@respx.mock
async def test_esearch_carries_the_human_filter(harvester: Any) -> None:
    """MeSH is the whole signal, so an unindexed record carries nothing."""
    respx.get(ESEARCH_URL).mock(
        return_value=httpx.Response(200, text=_esearch_body(0, []))
    )
    await harvester.count_and_ids(SEED, date(2026, 8, 21), date(2026, 9, 4))
    term = respx.calls.last.request.url.params["term"]
    assert '"humans"[MeSH Terms]' in term
    assert '"Dietary Supplements"[MeSH]' in term


@respx.mock
async def test_api_key_is_sent_when_present() -> None:
    """Raises the ceiling 3 -> 10 rps and makes the traffic attributable."""
    respx.get(ESEARCH_URL).mock(
        return_value=httpx.Response(200, text=_esearch_body(0, []))
    )
    async with httpx.AsyncClient() as client:
        keyed = PubMedIndexHarvester(client, api_key="secret")
        await keyed.count_and_ids(SEED, date(2026, 8, 21), date(2026, 9, 4))
    assert respx.calls.last.request.url.params["api_key"] == "secret"


@respx.mock
async def test_paging_is_by_retstart(harvester: Any) -> None:
    """The total comes back with the first page, so paging needs no extra call."""
    respx.get(ESEARCH_URL).mock(
        return_value=httpx.Response(200, text=_esearch_body(1200, ["1"]))
    )
    total, _ = await harvester.count_and_ids(
        SEED, date(2026, 8, 21), date(2026, 9, 4), retstart=500, retmax=500
    )
    assert total == 1200
    params = respx.calls.last.request.url.params
    assert params["retstart"] == "500"
    assert params["retmax"] == "500"


# --- efetch ------------------------------------------------------------------


@respx.mock
async def test_fetch_mesh_parses_the_fixture(harvester: Any) -> None:
    respx.get(EFETCH_URL).mock(return_value=httpx.Response(200, text=FIXTURE))

    records = await harvester.fetch_mesh(["42690769", "42682565"])

    # Four of five: the unindexed record carries no headings to count.
    assert {record.pmid for record in records} == {
        "42690769",
        "42693329",
        "42669527",
        "42028948",
    }


@respx.mock
async def test_fetch_mesh_batches(harvester: Any) -> None:
    """One request per 200 PMIDs — above that efetch needs a POST."""
    route = respx.get(EFETCH_URL).mock(return_value=httpx.Response(200, text=FIXTURE))

    await harvester.fetch_mesh([str(n) for n in range(EFETCH_BATCH_SIZE + 5)])

    assert route.call_count == 2
    first = route.calls[0].request.url.params["id"].split(",")
    second = route.calls[1].request.url.params["id"].split(",")
    assert len(first) == EFETCH_BATCH_SIZE
    assert len(second) == 5


@respx.mock
async def test_fetch_mesh_of_nothing_makes_no_request(harvester: Any) -> None:
    route = respx.get(EFETCH_URL).mock(return_value=httpx.Response(200, text=FIXTURE))
    assert await harvester.fetch_mesh([]) == []
    assert route.call_count == 0


# --- a throttled sweep must not look like an empty one ----------------------


@respx.mock
async def test_a_429_raises(harvester: Any) -> None:
    respx.get(ESEARCH_URL).mock(return_value=httpx.Response(429))
    with pytest.raises(RateLimited):
        await harvester.count_and_ids(SEED, date(2026, 8, 21), date(2026, 9, 4))


@respx.mock
async def test_a_200_carrying_a_rate_limit_message_raises(harvester: Any) -> None:
    """NCBI answers overage with a 200 as often as with a 429.

    Reading that body as an empty result set is how a throttled sweep reports
    "no trends this fortnight" — detected through the same ``detect_throttle``
    the pipeline's provider uses, so the two cannot disagree.
    """
    respx.get(ESEARCH_URL).mock(
        return_value=httpx.Response(
            200, text='{"error":"API rate limit exceeded for 1.2.3.4"}'
        )
    )
    with pytest.raises(RateLimited):
        await harvester.count_and_ids(SEED, date(2026, 8, 21), date(2026, 9, 4))


@respx.mock
async def test_a_server_error_raises(harvester: Any) -> None:
    respx.get(EFETCH_URL).mock(return_value=httpx.Response(500))
    with pytest.raises(ProviderError):
        await harvester.fetch_mesh(["1"])


@respx.mock
async def test_unparseable_xml_raises(harvester: Any) -> None:
    respx.get(EFETCH_URL).mock(return_value=httpx.Response(200, text="<not-xml"))
    with pytest.raises(ProviderError):
        await harvester.fetch_mesh(["1"])


@respx.mock
async def test_non_json_esearch_raises(harvester: Any) -> None:
    respx.get(ESEARCH_URL).mock(return_value=httpx.Response(200, text="<html>"))
    with pytest.raises(ProviderError):
        await harvester.count_and_ids(SEED, date(2026, 8, 21), date(2026, 9, 4))


@respx.mock
async def test_a_transport_failure_raises(harvester: Any) -> None:
    respx.get(ESEARCH_URL).mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(ProviderError):
        await harvester.count_and_ids(SEED, date(2026, 8, 21), date(2026, 9, 4))
