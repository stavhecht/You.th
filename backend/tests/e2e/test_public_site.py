"""The public site, from a reader's side: what the feed, search, facets and
article pages answer, from an empty site up to paging through dozens of
articles. Articles are published through the real ``approve()``.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.domain.enums import Verdict
from tests.e2e.support import seed_published


def _slugs(client: Any, **params: Any) -> list[str]:
    response = client.get("/api/feed", params=params)
    assert response.status_code == 200, response.text
    return [card["slug"] for card in response.json()["items"]]


# --- basics: an empty site works -------------------------------------------------


def test_an_empty_site_answers_every_public_route(client: Any) -> None:
    assert client.get("/api/healthz").json() == {"status": "ok"}
    feed = client.get("/api/feed").json()
    assert feed["items"] == [] and feed["nextCursor"] is None
    facets = client.get("/api/feed/facets").json()
    assert facets["total"] == 0
    assert client.get("/api/feed", params={"q": "anything"}).json()["items"] == []
    assert client.get("/api/articles/no-such-article").status_code == 404


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 51},
        {"limit": "many"},
        {"verdict": "proven"},
        {"subject": "astrology"},
        {"q": "x" * 201},
    ],
)
def test_bad_feed_parameters_are_refused_not_guessed(
    client: Any, params: dict[str, Any]
) -> None:
    assert client.get("/api/feed", params=params).status_code == 422


@pytest.mark.parametrize(
    "cursor",
    [
        "not-base64!!",
        "aGVsbG8=",
        "MHx4fHg=",
        "c2VhcmNofHRleHR8LTE=",
    ],  # junk, 'hello', '0|x|x', search -1
)
def test_a_tampered_cursor_is_a_400(client: Any, cursor: str) -> None:
    assert client.get("/api/feed", params={"cursor": cursor}).status_code == 400
    assert client.get("/api/feed", params={"cursor": cursor, "q": "a"}).status_code == 400


@pytest.mark.parametrize(
    "path",
    [
        "/api/media/zz/" + "0" * 62 + ".png",  # shard not hex
        "/api/media/00/" + "0" * 62 + ".svg",  # a format we never store
        "/api/media/00/" + "0" * 62 + ".png",  # well formed, not stored
        "/api/media/00/../../etc/passwd",
    ],
)
def test_every_kind_of_media_miss_is_the_same_404(client: Any, path: str) -> None:
    assert client.get(path).status_code == 404


def test_the_security_headers_are_on_every_response(client: Any) -> None:
    headers = client.get("/api/feed").headers
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"


# --- the human gate, seen from outside -------------------------------------------


async def test_only_published_articles_are_ever_visible(
    client: Any, factory: Any, reviewer: str
) -> None:
    _, live = await seed_published(factory, reviewer, headline="Zinc and colds")
    hidden = {
        status: (
            await seed_published(factory, reviewer, headline=f"Zinc {status}", status=status)
        )[1]
        for status in ("pending_review", "rejected", "validation_failed")
    }
    assert _slugs(client) == [live]
    assert _slugs(client, q="zinc") == [live]
    assert client.get("/api/feed/facets").json()["total"] == 1
    for slug in hidden.values():
        assert client.get(f"/api/articles/{slug}").status_code == 404


# --- reading at scale --------------------------------------------------------


async def test_paging_the_feed_returns_every_article_once_newest_first(
    client: Any, factory: Any, reviewer: str
) -> None:
    published = [
        (await seed_published(factory, reviewer, headline=f"Article number {n}"))[1]
        for n in range(30)
    ]
    seen: list[str] = []
    cursor = None
    while True:
        params: dict[str, Any] = {"limit": 7}
        if cursor:
            params["cursor"] = cursor
        page = client.get("/api/feed", params=params).json()
        seen += [card["slug"] for card in page["items"]]
        cursor = page["nextCursor"]
        if not cursor:
            break
    assert seen == list(reversed(published))


async def test_filters_narrow_and_facets_count_what_they_narrow_to(
    client: Any, factory: Any, reviewer: str
) -> None:
    rows = [
        ("Creatine and strength", Verdict.SUPPORTED, "supplements"),
        ("Creatine and memory", Verdict.MIXED, "supplements"),
        ("Foam rolling", Verdict.WEAK, "fitness"),
        ("Cold plunges", Verdict.NO_EVIDENCE, None),
    ]
    slugs = {
        h: (await seed_published(factory, reviewer, headline=h, verdict=v, subject=s))[1]
        for h, v, s in rows
    }

    assert set(_slugs(client, subject="supplements")) == {
        slugs["Creatine and strength"],
        slugs["Creatine and memory"],
    }
    assert _slugs(client, verdict="weak") == [slugs["Foam rolling"]]
    assert _slugs(client, subject="supplements", verdict="mixed") == [
        slugs["Creatine and memory"]
    ]
    assert _slugs(client, subject="sleep_recovery") == []

    facets = client.get("/api/feed/facets").json()
    assert facets["total"] == 4
    assert facets["subjects"].get("supplements") == 2
    assert facets["verdicts"].get("no_evidence") == 1


async def test_search_ranks_matches_and_falls_back_to_similar_titles(
    client: Any, factory: Any, reviewer: str
) -> None:
    _, ashwa = await seed_published(factory, reviewer, headline="Ashwagandha and stress")
    await seed_published(factory, reviewer, headline="Melatonin and jet lag")

    exact = client.get("/api/feed", params={"q": "ashwagandha"}).json()
    assert [c["slug"] for c in exact["items"]] == [ashwa]
    assert exact["match"] == "text"

    typo = client.get("/api/feed", params={"q": "ashwaganda"}).json()
    assert [c["slug"] for c in typo["items"]] == [ashwa]
    assert typo["match"] == "similar"

    assert _slugs(client, q="ashwagandha -stress") == []  # an exclusion is honoured
    assert _slugs(client, q="   ") == _slugs(client)  # blank is the feed


async def test_a_signed_in_reader_sees_their_subjects_first_but_never_fewer(
    client: Any, factory: Any, reviewer: str
) -> None:
    _, fitness = await seed_published(
        factory, reviewer, headline="Old fitness", subject="fitness"
    )
    _, other = await seed_published(
        factory, reviewer, headline="Newer nutrition", subject="nutrition"
    )
    _, unclassified = await seed_published(factory, reviewer, headline="Newest unclassified")

    signup = client.post(
        "/api/readers/signup",
        json={
            "email": "fit@e2e.test",
            "password": "Reader-password-1",
            "displayName": "F",
            "interests": ["fitness"],
        },
    )
    me = {"Authorization": f"Bearer {signup.json()['access_token']}"}

    personal = client.get("/api/feed", headers=me).json()["items"]
    assert personal[0]["slug"] == fitness  # their subject leads
    assert {c["slug"] for c in personal} == {
        fitness,
        other,
        unclassified,
    }  # reordered, not narrowed

    plain = client.get("/api/feed", params={"personalise": "false"}, headers=me).json()["items"]
    assert [c["slug"] for c in plain] == [unclassified, other, fitness]

    stale = {"Authorization": "Bearer not.a.token"}
    assert client.get("/api/feed", headers=stale).status_code == 200  # degrades, never 401


async def test_the_article_page_carries_its_sources_and_disclaimer(
    client: Any, console: dict[str, str], run_worker: Any
) -> None:
    run = client.post(
        "/api/console/pipeline/runs", json={"topic": "Magnesium for sleep"}, headers=console
    ).json()
    await run_worker()
    article_id = client.get(f"/api/console/pipeline/runs/{run['id']}", headers=console).json()[
        "articleId"
    ]
    client.post(f"/api/console/articles/{article_id}/approve", headers=console)
    slug = client.get("/api/feed").json()["items"][0]["slug"]

    article = client.get(f"/api/articles/{slug}").json()
    assert article["disclaimer"]
    assert article["retractionNotice"] is False
    for source in article["sources"]:
        assert source["citationHandle"].startswith("S")
        assert source["pmid"] or source["doi"]
        assert source["retracted"] is False
    assert "validationReport" not in article and "originalContent" not in article
