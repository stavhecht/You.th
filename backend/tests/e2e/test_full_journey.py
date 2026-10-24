"""The whole product, end to end: a topic in, a published article out, a reader
who finds it and saves it.

One test walks the path every guarantee sits on, through the real HTTP API, the
real worker and orchestrator, and a real database:

    reviewer queues a topic -> worker generates a draft through all nine stages
    -> the draft is invisible to the public -> reviewer edits and approves it
    -> readers see exactly the approved text in the feed, the article page and
    search -> a reader saves it, exports their data, and deletes their account.
"""

from __future__ import annotations

import copy
from typing import Any

PASSWORD = "Reader-password-1"


def _public_slugs(client: Any, **params: Any) -> list[str]:
    response = client.get("/api/feed", params=params)
    assert response.status_code == 200, response.text
    return [card["slug"] for card in response.json()["items"]]


async def test_a_topic_becomes_an_approved_article_a_reader_can_find_and_save(
    client: Any, console: dict[str, str], run_worker: Any
) -> None:
    # 1. A reviewer queues a topic. Nothing public exists yet.
    queued = client.post(
        "/api/console/pipeline/runs", json={"topic": "Magnesium for sleep"}, headers=console
    )
    assert queued.status_code == 202, queued.text
    run_id = queued.json()["id"]
    assert queued.json()["status"] == "queued"
    assert _public_slugs(client) == []

    # 2. The worker generates the draft.
    await run_worker()

    run = client.get(f"/api/console/pipeline/runs/{run_id}", headers=console).json()
    assert run["status"] == "succeeded", run["error"]
    assert run["attempts"] == 1
    stages = [(s["stage"], s["status"]) for s in run["stages"]]
    assert [name for name, _ in stages] == [
        "extract",
        "retrieve",
        "rank",
        "full_text",
        "appraise",
        "synthesize",
        "illustrate",
        "validate",
        "persist",
    ]
    assert {status for _, status in stages} == {"succeeded"}
    assert run["inputTokens"] > 0  # the ledger recorded the model calls
    article_id = run["articleId"]
    assert article_id

    # 3. The draft waits for a human, and the public cannot see it.
    queue = client.get(
        "/api/console/articles", params={"status": "pending_review"}, headers=console
    ).json()
    assert [item["id"] for item in queue["items"]] == [article_id]

    draft = client.get(f"/api/console/articles/{article_id}", headers=console).json()
    assert draft["status"] == "pending_review"
    assert draft["validationReport"]["passed"] is True
    assert draft["editedContent"] is None
    handles = sorted(s["citationHandle"] for s in draft["sources"])
    assert handles == ["S1", "S2", "S3"]
    slug = draft["slug"]

    assert client.get(f"/api/articles/{slug}").status_code == 404
    assert _public_slugs(client) == []
    assert _public_slugs(client, q="magnesium") == []

    # 4. The reviewer edits the wording (citations kept) and approves.
    edited = copy.deepcopy(draft["originalContent"])
    beat_one = next(n for n in edited["content"] if (n.get("attrs") or {}).get("beat") == 1)
    beat_one["content"] = [{"type": "text", "text": "Reviewer-edited opening line."}]
    saved = client.patch(
        f"/api/console/articles/{article_id}/content", json={"content": edited}, headers=console
    )
    assert saved.status_code == 204, saved.text

    preview = client.get(f"/api/console/articles/{article_id}/card", headers=console)
    assert preview.status_code == 200

    approved = client.post(f"/api/console/articles/{article_id}/approve", headers=console)
    assert approved.status_code == 204, approved.text

    # 5. Readers see exactly what was approved, everywhere they can look.
    assert _public_slugs(client) == [slug]
    card = client.get("/api/feed").json()["items"][0]
    assert card["headline"] == preview.json()["headline"]
    assert card["verdict"] == draft["verdict"]

    article = client.get(f"/api/articles/{slug}")
    assert article.status_code == 200
    body = article.json()
    assert body["content"] == edited  # the edit, not the machine draft
    cited = {h for c in body["citations"] for h in c["handles"]}
    assert cited <= {s["citationHandle"] for s in body["sources"]}  # every citation resolves
    assert all(s["title"] for s in body["sources"])

    assert _public_slugs(client, q="magnesium") == [slug]
    assert _public_slugs(client, q="reviewer-edited") == [slug]  # the edited body is indexed
    assert client.get("/api/feed/facets").json()["total"] == 1

    # 6. Published means frozen to the editor: no edits, no rejection.
    assert (
        client.patch(
            f"/api/console/articles/{article_id}/content",
            json={"content": edited},
            headers=console,
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/api/console/articles/{article_id}/reject",
            json={"reason": "late"},
            headers=console,
        ).status_code
        == 409
    )

    # 7. A reader signs up, saves it, takes their data, and leaves.
    signup = client.post(
        "/api/readers/signup",
        json={"email": "Reader@E2E.test", "password": PASSWORD, "displayName": "Ada"},
    )
    assert signup.status_code == 201, signup.text
    me = {"Authorization": f"Bearer {signup.json()['access_token']}"}

    assert client.put(f"/api/readers/saved/{slug}", json={}, headers=me).status_code == 204
    assert client.get("/api/readers/saved/slugs", headers=me).json() is not None
    shelf = client.get("/api/readers/saved", headers=me).json()
    assert [item["slug"] for item in shelf["items"]] == [slug]

    export = client.get("/api/readers/me/export", headers=me)
    assert export.status_code == 200
    assert "attachment" in export.headers["content-disposition"]
    assert slug in export.text
    assert "reader@e2e.test" in export.text  # stored lower-cased

    wrong = client.request("DELETE", "/api/readers/me", json={"password": "nope"}, headers=me)
    assert wrong.status_code == 403
    gone = client.request("DELETE", "/api/readers/me", json={"password": PASSWORD}, headers=me)
    assert gone.status_code == 204
    assert client.get("/api/readers/me", headers=me).status_code == 401  # token died with it
    assert _public_slugs(client) == [slug]  # the article does not depend on the reader
