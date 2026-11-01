"""Reader accounts over HTTP: signing up, signing in, shelves, and isolation.

Readers are the public's only write path, so every rule here is checked from
the outside: what a malformed request gets back, and that one reader can never
see or change another's data.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.e2e.support import seed_published

PASSWORD = "Reader-password-1"


def _signup(client: Any, email: str, **extra: Any) -> dict[str, str]:
    response = client.post(
        "/api/readers/signup",
        json={"email": email, "password": PASSWORD, "displayName": "R", **extra},
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


# --- signing up --------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("password", "short-1A"[:7]),  # under eight characters
        ("password", "alllowercase-1"),  # no capital
        ("password", "ALLUPPERCASE-1"),  # no lowercase
        ("password", "No-digits-here"),  # no number
        ("password", "NoSpecial123"),  # no special character
        ("email", "not-an-email"),
        ("email", "two@@example.com"),
        ("displayName", ""),
        ("interests", ["astrology"]),
    ],
)
def test_signup_refuses_bad_input(client: Any, field: str, value: Any) -> None:
    body = {"email": "new@e2e.test", "password": PASSWORD, "displayName": "N", field: value}
    assert client.post("/api/readers/signup", json=body).status_code == 422


def test_one_address_is_one_account_whatever_its_case(client: Any) -> None:
    _signup(client, "Same@E2E.test")
    again = client.post(
        "/api/readers/signup",
        json={"email": "  same@e2e.TEST ", "password": PASSWORD, "displayName": "Twin"},
    )
    assert again.status_code == 409


def test_signing_in_works_and_failures_do_not_say_why(client: Any) -> None:
    _signup(client, "ada@e2e.test")
    ok = client.post("/api/readers/login", json={"email": "ADA@e2e.test", "password": PASSWORD})
    assert ok.status_code == 200
    wrong = client.post("/api/readers/login", json={"email": "ada@e2e.test", "password": "x"})
    unknown = client.post("/api/readers/login", json={"email": "bob@e2e.test", "password": "x"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


def test_profile_updates_are_patches(client: Any) -> None:
    me = _signup(client, "pat@e2e.test", interests=["fitness", "sleep_recovery"])
    client.patch("/api/readers/me", json={"newsletter": True}, headers=me)
    profile = client.get("/api/readers/me", headers=me).json()
    assert profile["newsletter"] is True
    assert set(profile["interests"]) == {"fitness", "sleep_recovery"}  # untouched

    client.patch("/api/readers/me", json={"interests": []}, headers=me)  # [] clears
    assert client.get("/api/readers/me", headers=me).json()["interests"] == []


# --- shelves -----------------------------------------------------------------


async def test_saving_moving_and_unsaving(client: Any, factory: Any, reviewer: str) -> None:
    _, slug = await seed_published(factory, reviewer, headline="Saved thing")
    me = _signup(client, "shelf@e2e.test")

    assert client.put(f"/api/readers/saved/{slug}", json={}, headers=me).status_code == 204
    reading = client.post(
        "/api/readers/folders", json={"name": "Reading list"}, headers=me
    ).json()
    # Saving again into another folder moves it rather than duplicating it.
    moved = client.put(
        f"/api/readers/saved/{slug}", json={"folderId": reading["id"]}, headers=me
    )
    assert moved.status_code == 204
    folders = {
        f["name"]: f["count"] for f in client.get("/api/readers/folders", headers=me).json()
    }
    assert folders == {"Saved": 0, "Reading list": 1}
    assert client.get("/api/readers/saved/slugs", headers=me).json() == {slug: reading["id"]}

    assert client.delete(f"/api/readers/saved/{slug}", headers=me).status_code == 204
    assert client.get("/api/readers/saved", headers=me).json()["items"] == []


async def test_only_published_articles_can_be_saved(
    client: Any, factory: Any, reviewer: str
) -> None:
    """A save that worked for a draft's slug would be a probe for unpublished work."""
    _, draft = await seed_published(
        factory, reviewer, headline="Draft", status="pending_review"
    )
    me = _signup(client, "probe@e2e.test")
    assert client.put(f"/api/readers/saved/{draft}", json={}, headers=me).status_code == 409
    assert client.put("/api/readers/saved/no-such-slug", json={}, headers=me).status_code == 409


def test_folder_rules(client: Any) -> None:
    me = _signup(client, "folders@e2e.test")
    create = lambda name: client.post("/api/readers/folders", json={"name": name}, headers=me)  # noqa: E731

    assert create("   ").status_code in (409, 422)
    assert create("x" * 61).status_code in (409, 422)
    assert create("Later").status_code == 201
    assert create("Later").status_code == 409  # names are unique per reader

    folders = client.get("/api/readers/folders", headers=me).json()
    for folder in folders[:-1]:
        assert (
            client.delete(f"/api/readers/folders/{folder['id']}", headers=me).status_code == 204
        )
    last = client.get("/api/readers/folders", headers=me).json()
    assert len(last) == 1
    assert client.delete(f"/api/readers/folders/{last[0]['id']}", headers=me).status_code == 409


async def test_one_reader_can_never_reach_another_readers_shelf(
    client: Any, factory: Any, reviewer: str
) -> None:
    _, slug = await seed_published(factory, reviewer, headline="Private shelf")
    alice = _signup(client, "alice@e2e.test")
    bob = _signup(client, "bob@e2e.test")
    alices_folder = client.post(
        "/api/readers/folders", json={"name": "Alice only"}, headers=alice
    ).json()["id"]
    client.put(f"/api/readers/saved/{slug}", json={"folderId": alices_folder}, headers=alice)

    assert client.get("/api/readers/saved", headers=bob).json()["items"] == []
    assert (
        client.get(
            "/api/readers/saved", params={"folderId": alices_folder}, headers=bob
        ).json()["items"]
        == []
    )
    # Bob can neither delete Alice's folder nor file his own save into it.
    assert (
        client.delete(f"/api/readers/folders/{alices_folder}", headers=bob).status_code == 409
    )
    into = client.put(
        f"/api/readers/saved/{slug}", json={"folderId": alices_folder}, headers=bob
    )
    assert 400 <= into.status_code < 500, into.text
    assert client.get("/api/readers/saved/slugs", headers=alice).json() == {slug: alices_folder}


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("DELETE", "/api/readers/folders/not-a-uuid", None),
        ("GET", "/api/readers/saved?folderId=not-a-uuid", None),
        ("PUT", "/api/readers/saved/anything", {"folderId": "not-a-uuid"}),
    ],
)
def test_a_malformed_folder_id_is_a_client_error(
    client: Any, method: str, path: str, body: Any
) -> None:
    me = _signup(client, "ids@e2e.test")
    response = client.request(method, path, json=body, headers=me)
    assert 400 <= response.status_code < 500, (response.status_code, response.text[:200])


# --- leaving -----------------------------------------------------------------


async def test_deleting_an_account_removes_exactly_that_readers_data(
    client: Any, factory: Any, reviewer: str
) -> None:
    _, slug = await seed_published(factory, reviewer, headline="Shared article")
    leaver = _signup(client, "leaver@e2e.test")
    stayer = _signup(client, "stayer@e2e.test")
    for me in (leaver, stayer):
        client.put(f"/api/readers/saved/{slug}", json={}, headers=me)

    gone = client.request(
        "DELETE", "/api/readers/me", json={"password": PASSWORD}, headers=leaver
    )
    assert gone.status_code == 204
    again = client.post(
        "/api/readers/login", json={"email": "leaver@e2e.test", "password": PASSWORD}
    )
    assert again.status_code == 401
    assert client.get("/api/readers/saved/slugs", headers=stayer).json() != {}
    # The address is free again: erasure is not a ban.
    assert (
        client.post(
            "/api/readers/signup",
            json={"email": "leaver@e2e.test", "password": PASSWORD, "displayName": "Back"},
        ).status_code
        == 201
    )
