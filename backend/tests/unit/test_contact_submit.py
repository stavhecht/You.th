"""What "Let us know" refuses: no name, or neither a link nor a description."""

from typing import Any

import pytest

from app.domain.enums import ContactKind
from app.services.contact import ContactError, ContactService


class _Session:
    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, row: Any) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        return None


async def _submit(**fields: Any) -> _Session:
    session = _Session()
    base = {"kind": ContactKind.TOPIC, "email": "a@example.com", "name": "Ada", "note": ""}
    await ContactService(session).submit(**(base | fields))  # type: ignore[arg-type]
    return session


async def test_a_link_alone_is_enough() -> None:
    session = await _submit(link="https://example.com/video")
    assert session.added[0].note == ""
    assert session.added[0].link == "https://example.com/video"


async def test_a_description_alone_is_enough() -> None:
    session = await _submit(note="  creatine and sleep  ")
    assert session.added[0].note == "creatine and sleep"


@pytest.mark.parametrize(
    "fields",
    [{"note": "  ", "link": " "}, {"note": "hi", "name": "  "}, {"note": "hi", "name": None}],
)
async def test_missing_name_or_content_is_refused(fields: dict[str, Any]) -> None:
    with pytest.raises(ContactError):
        await _submit(**fields)


async def test_parallel_messages_cannot_outrun_the_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Counted after the save, every parallel request passed the check first."""
    import asyncio

    import httpx

    import app.api.deps as deps
    import app.api.public.contact as contact_api
    from app.main import create_app
    from app.security.login_throttle import IP_BUDGET, InMemoryLoginThrottle

    saved: list[Any] = []

    class _Session:
        def add(self, row: Any) -> None:
            row.id = f"00000000-0000-0000-0000-{len(saved):012d}"
            saved.append(row)

        async def flush(self) -> None:
            await asyncio.sleep(0.01)  # the database wait the race needs

        async def commit(self) -> None:
            return None

    async def _session() -> Any:
        yield _Session()

    async def _no_email(*_: Any) -> None:
        return None

    throttle = InMemoryLoginThrottle()
    app = create_app()
    app.dependency_overrides[deps.get_session] = _session
    app.dependency_overrides[deps.get_contact_throttle] = lambda: throttle
    app.dependency_overrides[deps.client_ip] = lambda: "203.0.113.7"
    monkeypatch.setattr(contact_api, "notify_editors", _no_email)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        responses = await asyncio.gather(
            *(
                http.post(
                    "/api/contact",
                    json={"name": "Ada", "email": f"a{i}@example.com", "note": "hi"},
                )
                for i in range(3 * IP_BUDGET.max_failures)
            )
        )

    accepted = [r for r in responses if r.status_code == 202]
    assert len(accepted) == len(saved) == IP_BUDGET.max_failures
    assert all(r.status_code in (202, 429) for r in responses)


async def test_only_a_signed_in_reader_can_suggest_a_topic_and_it_is_sent_as_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Topics are for accounts; the name and address come from the account, not the form."""
    from types import SimpleNamespace

    import httpx

    import app.api.deps as deps
    import app.api.public.contact as contact_api
    from app.main import create_app
    from app.security.auth import AuthenticatedReader

    saved: list[Any] = []

    class _Session:
        def add(self, row: Any) -> None:
            row.id = "3f1c0000-0000-0000-0000-000000000001"
            saved.append(row)

        async def flush(self) -> None:
            return None

        async def commit(self) -> None:
            return None

    async def _session() -> Any:
        yield _Session()

    async def _no_email(*_: Any) -> None:
        return None

    monkeypatch.setattr(contact_api, "notify_editors", _no_email)
    counted: list[str] = []
    allow = SimpleNamespace(
        retry_after=lambda **_: None,
        record_failure=lambda *, ip, email: counted.append(email),
    )
    signed_in: list[AuthenticatedReader | None] = [None]
    app = create_app()
    app.dependency_overrides[deps.get_session] = _session
    app.dependency_overrides[deps.get_contact_throttle] = lambda: allow
    app.dependency_overrides[deps.client_ip] = lambda: "203.0.113.7"
    app.dependency_overrides[deps.optional_reader] = lambda: signed_in[0]

    topic = {
        "kind": "topic",
        "name": "Someone",
        "email": "else@example.com",
        "note": "creatine",
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        guest = await http.post("/api/contact", json=topic)
        other = await http.post("/api/contact", json={**topic, "kind": "other"})
        signed_in[0] = AuthenticatedReader(id="r1", email="ada@example.com", display_name="Ada")
        member = await http.post("/api/contact", json=topic)

    assert guest.status_code == 401
    assert other.status_code == 202
    assert member.status_code == 202
    assert [row.kind for row in saved] == [ContactKind.OTHER, ContactKind.TOPIC]
    assert (saved[1].name, saved[1].email) == ("Ada", "ada@example.com")
    # Counted against the account, so changing the form's email cannot dodge the limit.
    assert counted[-1] == "ada@example.com"
