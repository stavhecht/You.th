"""The review desk over HTTP: the human gate every published word passes.

Drafts come from the real pipeline (``run_worker``) or are seeded and published
through the real ``approve()``; every action goes through the API a reviewer's
browser uses.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from tests.e2e.support import seed_published

UNKNOWN_ID = "00000000-0000-0000-0000-000000000000"


async def _draft(client: Any, console: dict[str, str], run_worker: Any) -> dict[str, Any]:
    run = client.post(
        "/api/console/pipeline/runs", json={"topic": "Magnesium for sleep"}, headers=console
    ).json()
    await run_worker()
    article_id = client.get(f"/api/console/pipeline/runs/{run['id']}", headers=console).json()[
        "articleId"
    ]
    return client.get(f"/api/console/articles/{article_id}", headers=console).json()


def _with_text(doc: dict[str, Any], beat: int, inline: list[dict[str, Any]]) -> dict[str, Any]:
    edited = copy.deepcopy(doc)
    node = next(n for n in edited["content"] if (n.get("attrs") or {}).get("beat") == beat)
    node["content"] = inline
    return edited


# --- signing in ----------------------------------------------------------------


def test_a_wrong_password_and_an_unknown_email_look_identical(
    client: Any, reviewer: str
) -> None:
    from tests.e2e.conftest import REVIEWER_EMAIL

    wrong = client.post(
        "/api/console/auth/login", json={"email": REVIEWER_EMAIL, "password": "Nope-nope-1"}
    )
    unknown = client.post(
        "/api/console/auth/login", json={"email": "who@e2e.test", "password": "Nope-nope-1"}
    )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()  # no hint which half was wrong


def test_a_reader_token_cannot_open_the_desk(client: Any, console: dict[str, str]) -> None:
    signup = client.post(
        "/api/readers/signup",
        json={"email": "r@e2e.test", "password": "Reader-password-1", "displayName": "R"},
    )
    reader = {"Authorization": f"Bearer {signup.json()['access_token']}"}
    assert client.get("/api/console/articles", headers=reader).status_code == 401
    assert client.get("/api/readers/me", headers=console).status_code == 401  # and back


# --- the gate ------------------------------------------------------------------


async def test_rejecting_a_draft_is_final_and_keeps_it_off_the_site(
    client: Any, console: dict[str, str], run_worker: Any
) -> None:
    draft = await _draft(client, console, run_worker)
    article = f"/api/console/articles/{draft['id']}"

    assert (
        client.post(f"{article}/reject", json={"reason": ""}, headers=console).status_code
        == 422
    )
    assert (
        client.post(
            f"{article}/reject",
            json={"reason": "Sources do not support the headline"},
            headers=console,
        ).status_code
        == 204
    )

    assert client.get(article, headers=console).json()["status"] == "rejected"
    assert client.post(f"{article}/approve", headers=console).status_code == 409
    assert (
        client.post(f"{article}/reject", json={"reason": "again"}, headers=console).status_code
        == 409
    )
    assert (
        client.patch(
            f"{article}/content", json={"content": draft["originalContent"]}, headers=console
        ).status_code
        == 409
    )
    assert client.get(f"/api/articles/{draft['slug']}").status_code == 404
    assert client.get("/api/feed").json()["items"] == []


async def test_an_edit_citing_an_unknown_source_cannot_be_published(
    client: Any, console: dict[str, str], run_worker: Any
) -> None:
    """Invariant #2 survives human editing: a pasted handle the article never
    had blocks Approve, names the handle, and leaves the draft pending."""
    draft = await _draft(client, console, run_worker)
    article = f"/api/console/articles/{draft['id']}"
    edited = _with_text(
        draft["originalContent"],
        2,
        [
            {"type": "text", "text": "A finding the reviewer typed in."},
            {"type": "citation", "attrs": {"sourceIds": ["S9"]}},
        ],
    )
    assert (
        client.patch(
            f"{article}/content", json={"content": edited}, headers=console
        ).status_code
        == 204
    )

    refused = client.post(f"{article}/approve", headers=console)
    assert refused.status_code == 409
    assert "S9" in refused.json()["detail"]
    assert client.get(article, headers=console).json()["status"] == "pending_review"
    assert client.get("/api/feed").json()["items"] == []


async def test_an_edit_embedding_a_foreign_image_is_refused(
    client: Any, console: dict[str, str], run_worker: Any
) -> None:
    """A document may only reference media this system stored: anything else is
    a third-party request on every reader's page view."""
    draft = await _draft(client, console, run_worker)
    edited = copy.deepcopy(draft["originalContent"])
    edited["content"].append(
        {"type": "image", "attrs": {"src": "https://tracker.example/pixel.png", "alt": "x"}}
    )
    response = client.patch(
        f"/api/console/articles/{draft['id']}/content",
        json={"content": edited},
        headers=console,
    )
    assert response.status_code == 409
    detail = client.get(f"/api/console/articles/{draft['id']}", headers=console).json()
    assert detail["editedContent"] is None  # nothing was saved


async def test_the_original_draft_is_kept_however_much_the_reviewer_edits(
    client: Any, console: dict[str, str], run_worker: Any
) -> None:
    """Invariant #4: edits go to ``editedContent``; the machine draft is the
    permanent record of what the model wrote."""
    draft = await _draft(client, console, run_worker)
    article = f"/api/console/articles/{draft['id']}"
    for n in range(3):
        edited = _with_text(
            draft["originalContent"], 1, [{"type": "text", "text": f"Edit {n}."}]
        )
        assert (
            client.patch(
                f"{article}/content", json={"content": edited}, headers=console
            ).status_code
            == 204
        )

    detail = client.get(article, headers=console).json()
    assert detail["originalContent"] == draft["originalContent"]
    assert detail["editedContent"] == edited


async def test_the_card_preview_is_what_approve_publishes(
    client: Any, console: dict[str, str], run_worker: Any
) -> None:
    draft = await _draft(client, console, run_worker)
    preview = client.get(f"/api/console/articles/{draft['id']}/card", headers=console).json()
    client.post(f"/api/console/articles/{draft['id']}/approve", headers=console)
    card = client.get("/api/feed").json()["items"][0]
    for key in ("slug", "headline", "excerpt", "verdict"):
        assert card[key] == preview[key], key


# --- metadata after publication ----------------------------------------------


async def test_the_subject_can_be_corrected_after_publication(
    client: Any, console: dict[str, str], factory: Any, reviewer: str
) -> None:
    article_id, slug = await seed_published(factory, reviewer, headline="Creatine and memory")
    route = f"/api/console/articles/{article_id}/subject"

    assert (
        client.patch(route, json={"subject": "supplements"}, headers=console).status_code == 204
    )
    assert (
        client.get("/api/feed", params={"subject": "supplements"}).json()["items"][0]["slug"]
        == slug
    )
    assert client.get("/api/feed", params={"subject": "sleep_recovery"}).json()["items"] == []

    assert client.patch(route, json={"subject": None}, headers=console).status_code == 204
    assert client.get("/api/feed").json()["items"][0]["subject"] is None
    assert (
        client.patch(route, json={"subject": "astrology"}, headers=console).status_code == 422
    )


async def test_a_rejected_article_cannot_be_classified(
    client: Any, console: dict[str, str], factory: Any, reviewer: str
) -> None:
    article_id, _ = await seed_published(
        factory, reviewer, headline="Rejected thing", status="rejected"
    )
    response = client.patch(
        f"/api/console/articles/{article_id}/subject",
        json={"subject": "fitness"},
        headers=console,
    )
    assert response.status_code == 409


# --- lists and ids -------------------------------------------------------------


def test_the_run_list_pages_without_gaps_or_repeats(
    client: Any, console: dict[str, str]
) -> None:
    created = [
        client.post(
            "/api/console/pipeline/runs", json={"topic": f"Topic {n}"}, headers=console
        ).json()["id"]
        for n in range(5)
    ]
    seen: list[str] = []
    cursor = None
    for _ in range(5):
        params: dict[str, Any] = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        page = client.get("/api/console/pipeline/runs", params=params, headers=console).json()
        seen += [run["id"] for run in page["items"]]
        cursor = page["nextCursor"]
        if not cursor:
            break
    assert seen == list(reversed(created))  # newest first, each exactly once


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/api/console/articles/{id}", None),
        ("GET", "/api/console/articles/{id}/card", None),
        (
            "PATCH",
            "/api/console/articles/{id}/content",
            {"content": {"type": "doc", "content": []}},
        ),
        ("PATCH", "/api/console/articles/{id}/subject", {"subject": None}),
        ("POST", "/api/console/articles/{id}/approve", None),
        ("POST", "/api/console/articles/{id}/reject", {"reason": "x"}),
        ("GET", "/api/console/pipeline/runs/{id}", None),
        ("PATCH", "/api/console/contact/{id}", {"status": "closed"}),
    ],
)
@pytest.mark.parametrize("bad_id", [UNKNOWN_ID, "not-a-uuid"])
def test_an_unknown_or_malformed_id_is_a_client_error_never_a_500(
    client: Any, console: dict[str, str], method: str, path: str, body: Any, bad_id: str
) -> None:
    response = client.request(method, path.format(id=bad_id), json=body, headers=console)
    assert 400 <= response.status_code < 500, (response.status_code, response.text[:200])
