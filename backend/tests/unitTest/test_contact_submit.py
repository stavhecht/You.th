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
